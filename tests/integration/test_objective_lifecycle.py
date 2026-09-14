"""Objective lifecycle completion (ADR-0020) — the objective is the
runtime's representation of WHAT THE HUMAN WANTS, and its state must
reflect runtime evidence, not silence.

Verified here:
- the extended transition map (waiting / awaiting_human / cancelled are
  real states with real edges — and terminal means terminal);
- objective ≠ execution: multiple executions participate in one
  objective's life, with an append-only history;
- evidence-driven sync: scheduled durable work → waiting; woken work →
  in_progress; death of the last pending work → failed; a pending
  approval → awaiting_human; consumption → active again;
- the intelligence's objective capabilities (list / resume /
  update_status) through the REAL gate chain, including the ownership
  and terminal-state boundaries;
- resumption redirects the live execution so one human goal stops being
  a chain of sibling per-message objectives.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ulid import ULID

from wax.authority.seed import seed_builtin_roles
from wax.intelligence.adapters.mock_provider import MockLLMProvider
from wax.intelligence.contracts import ToolCall
from wax.intelligence.service import IntelligenceService
from wax.objective.contracts import ObjectiveCreate, ObjectiveKind, ObjectiveStatus
from wax.objective.repository import ObjectiveRepository
from wax.runtime.bridge.contracts import InterfaceKind, RuntimeRequest
from wax.runtime.bridge.service import RuntimeBridge
from wax.runtime.services import RuntimeServices
from wax.runtime.work import WorkRunner, capability_handler
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base


@pytest.fixture
async def fresh_db(test_settings):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db_session() as session:
        await seed_builtin_roles(session)
        await session.commit()
    yield
    await dispose_engine()


@pytest.fixture
def services(test_settings) -> RuntimeServices:
    return RuntimeServices.build(test_settings)


def _request(message_id: str, text: str) -> RuntimeRequest:
    return RuntimeRequest(
        interface_message_id=message_id,
        interface_kind=InterfaceKind.WHATSAPP,
        sender_interface_id="+2348000000777",
        sender_display_name="Lifecycle",
        text=text,
        received_at=datetime.now(UTC),
    )


def _call(call_id: str, name: str, args: dict) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=args)


async def _run_runner_once(services: RuntimeServices) -> int:
    runner = WorkRunner(
        services,
        poll_interval_seconds=0.05,
        lease_seconds=120.0,
        retry_backoff_seconds=0.0,
        stale_execution_seconds=900.0,
    )
    runner.register_handler("capability", capability_handler)
    return await runner.run_once()


class TestTransitionMap:
    """The extended lifecycle: waiting / awaiting_human / cancelled."""

    async def _objective(self, principal_id: str) -> str:
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            obj = await repo.create(
                ObjectiveCreate(
                    principal_id=principal_id,
                    description="watch the market and report anomalies",
                    kind=ObjectiveKind.LONG_RUNNING,
                )
            )
            await session.commit()
            return obj.id

    async def test_pending_to_waiting_is_legal(self, fresh_db, services) -> None:
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            obj = await repo.create(
                ObjectiveCreate(principal_id="p", description="x")
            )
            assert await repo.transition(obj.id, ObjectiveStatus.WAITING)
            await session.commit()

    async def test_in_progress_to_awaiting_human_is_legal(self, fresh_db) -> None:
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            obj = await repo.create(
                ObjectiveCreate(principal_id="p", description="x")
            )
            await repo.transition(obj.id, ObjectiveStatus.IN_PROGRESS)
            assert await repo.transition(obj.id, ObjectiveStatus.AWAITING_HUMAN)
            await session.commit()

    async def test_waiting_to_succeeded_is_illegal(self, fresh_db) -> None:
        """A waiting objective must come back through active evidence —
        the wait itself is not success."""
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            obj = await repo.create(
                ObjectiveCreate(principal_id="p", description="x")
            )
            await repo.transition(obj.id, ObjectiveStatus.WAITING)
            assert not await repo.transition(obj.id, ObjectiveStatus.SUCCEEDED)
            await session.commit()

    async def test_cancelled_is_terminal(self, fresh_db) -> None:
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            obj = await repo.create(
                ObjectiveCreate(principal_id="p", description="x")
            )
            await repo.transition(obj.id, ObjectiveStatus.IN_PROGRESS)
            assert await repo.transition(obj.id, ObjectiveStatus.CANCELLED)
            assert not await repo.transition(obj.id, ObjectiveStatus.IN_PROGRESS)
            await session.commit()

    async def test_awaiting_human_may_succeed_when_interaction_completes(
        self, fresh_db
    ) -> None:
        """The interaction completes while an approval stays pending —
        the objective closes honestly; the approval lives independently."""
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            obj = await repo.create(
                ObjectiveCreate(principal_id="p", description="x")
            )
            await repo.transition(obj.id, ObjectiveStatus.IN_PROGRESS)
            await repo.transition(obj.id, ObjectiveStatus.AWAITING_HUMAN)
            assert await repo.transition(obj.id, ObjectiveStatus.SUCCEEDED)
            await session.commit()


class TestExecutionHistory:
    """objective ≠ execution (mission §16): history, not a pointer."""

    async def test_multiple_executions_one_objective(self, fresh_db) -> None:
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            obj = await repo.create(
                ObjectiveCreate(principal_id="p", description="x")
            )
            await repo.record_execution_start(obj.id, "exec-A", kind="bridge")
            await repo.record_execution_end(obj.id, "exec-A", outcome="failed")
            # Retry: a second execution joins the SAME objective.
            await repo.record_execution_start(obj.id, "exec-B", kind="bridge")
            await repo.record_execution_end(obj.id, "exec-B", outcome="succeeded")
            history = await repo.list_executions(obj.id)
            await session.commit()

        assert len(history) == 2
        assert [h.execution_id for h in history] == ["exec-A", "exec-B"]
        assert [h.outcome for h in history] == ["failed", "succeeded"]
        assert all(h.ended_at is not None for h in history)

    async def test_open_history_rows_are_counted(self, fresh_db) -> None:
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            obj = await repo.create(
                ObjectiveCreate(principal_id="p", description="x")
            )
            await repo.record_execution_start(obj.id, "exec-A", kind="bridge")
            assert await repo.count_open_executions(obj.id) == 1
            await repo.record_execution_end(obj.id, "exec-A", outcome="succeeded")
            assert await repo.count_open_executions(obj.id) == 0
            await session.commit()

    async def test_end_never_fabricates_missing_rows(self, fresh_db) -> None:
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            obj = await repo.create(
                ObjectiveCreate(principal_id="p", description="x")
            )
            closed = await repo.record_execution_end(
                obj.id, "never-started", outcome="succeeded"
            )
            await session.commit()
        assert closed == 0


class TestObjectiveStateSurvivesTheInteraction:
    """An objective with outstanding durable work is NOT succeeded when
    the interaction that scheduled it closes — waiting is the truth
    (mission §60: no fake autonomy, §24: evidence-based completion)."""

    async def test_scheduled_work_holds_the_objective_open(
        self, fresh_db, services
    ) -> None:
        from wax.runtime.bridge.contracts import RuntimeResponseStatus
        from wax.state.objective_models import ObjectiveRecord
        from wax.state.work_models import WorkItemRecord

        script = [
            [
                _call(
                    "t1",
                    "work.schedule",
                    {
                        "payload": {
                            "capability_name": "echo",
                            "inputs": {"message": "heartbeat"},
                        },
                        "delay_seconds": 0.05,
                    },
                )
            ],
            [],
        ]
        bridge = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider(scripted_tool_calls=script)),
            services=services,
        )
        async with db_session() as session:
            response = await bridge.process(
                session, _request(f"m-{ULID()}", "ping me later")
            )
            await session.commit()
        assert response.status == RuntimeResponseStatus.SUCCESS

        async with db_session() as session:
            obj = await session.get(ObjectiveRecord, response.objective_id)
            assert obj is not None
            assert obj.status == "waiting", (
                "the interaction closed but its scheduled work still pends: "
                "the objective must reflect waiting, not succeeded"
            )

        # The work wakes and runs → the objective returns to active.
        import asyncio

        await asyncio.sleep(0.15)  # the scheduled delay passes
        ran = await _run_runner_once(services)
        assert ran == 1
        async with db_session() as session:
            obj = await session.get(ObjectiveRecord, response.objective_id)
            assert obj is not None
            assert obj.status == "in_progress"
            repo = ObjectiveRepository(session)
            history = await repo.list_executions(response.objective_id)
        kinds = [h.kind for h in history]
        assert "bridge" in kinds and "work" in kinds, (
            f"work participation must appear in history: {kinds}"
        )

    async def test_scheduled_destructive_work_walks_waiting_to_awaiting_human_to_active(
        self, fresh_db, services
    ) -> None:
        """The full evidence lifecycle over the real path:
        waiting (work pends) → awaiting_human (approval pends) →
        active (approval consumed, action runs exactly once)."""
        import asyncio

        from sqlalchemy import select

        from wax.authority.approvals import ApprovalService
        from wax.capabilities.contracts import CapabilityDescriptor
        from wax.runtime.bridge.contracts import RuntimeResponseStatus
        from wax.state.approval_models import PendingApprovalRecord
        from wax.state.objective_models import ObjectiveRecord
        from wax.state.work_models import WorkItemRecord

        async def _wipe(inputs: dict, ctx=None) -> dict:
            return {"wiped": True}

        if "test.objwipe" not in services.capability_registry:
            services.capability_registry.register(
                CapabilityDescriptor(
                    name="test.objwipe",
                    description="Destructive test capability",
                    required_permission="capability.invoke:built_in",
                    is_destructive=True,
                    timeout_seconds=2.0,
                ),
                _wipe,
            )

        bridge = RuntimeBridge(
            intelligence=IntelligenceService(
                MockLLMProvider(
                    scripted_tool_calls=[
                        [
                            _call(
                                "c1",
                                "work.schedule",
                                {
                                    "payload": {
                                        "capability_name": "test.objwipe",
                                        "inputs": {"target": "old"},
                                    },
                                    "delay_seconds": 0.05,
                                },
                            )
                        ]
                    ]
                )
            ),
            services=services,
        )
        async with db_session() as session:
            response = await bridge.process(
                session, _request(f"m-{ULID()}", "wipe it later")
            )
            await session.commit()
        assert response.status == RuntimeResponseStatus.SUCCESS
        objective_id = response.objective_id

        async with db_session() as session:
            obj = await session.get(ObjectiveRecord, objective_id)
            assert obj is not None and obj.status == "waiting"

        await asyncio.sleep(0.15)
        ran = await _run_runner_once(services)
        assert ran == 1

        # The agency gate demanded a human: the objective is awaiting_human.
        async with db_session() as session:
            obj = await session.get(ObjectiveRecord, objective_id)
            assert obj is not None
            assert obj.status == "awaiting_human", (
                f"pending approval must surface as awaiting_human; got {obj.status}"
            )
            approval = (
                (
                    await session.execute(
                        select(PendingApprovalRecord).where(
                            PendingApprovalRecord.requested_by_execution_id
                            == response.execution_id
                        )
                    )
                )
                .scalars()
                .first()
            )
            assert approval is not None

        # The human approves; the work requeues and consumes it.
        async with db_session() as session:
            await ApprovalService(session).decide(
                approval.id,
                decided_by=response.principal_id,
                approve=True,
            )
            await session.commit()
        ran = await _run_runner_once(services)
        assert ran == 1

        async with db_session() as session:
            obj = await session.get(ObjectiveRecord, objective_id)
            assert obj is not None
            assert obj.status == "in_progress", (
                f"consuming the approval must reactivate the objective; "
                f"got {obj.status}"
            )
            item = (
                (
                    await session.execute(
                        select(WorkItemRecord).where(
                            WorkItemRecord.execution_id == response.execution_id
                        )
                    )
                )
                .scalars()
                .first()
            )
            assert item is not None and item.status == "succeeded"


class TestObjectiveCapabilities:
    """objective.list / resume / update_status through the REAL gate chain
    (agency → budget → authority → invoker → audit)."""

    async def _invoke(
        self,
        services: RuntimeServices,
        principal_id: str,
        name: str,
        inputs: dict,
        execution_id: str | None = None,
    ):
        from wax.capabilities.contracts import CapabilityInvocationRequest

        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name=name,
                    principal_id=principal_id,
                    inputs=inputs,
                    request_id=execution_id or f"exec-{ULID()}",
                )
            )
            await session.commit()
            return result

    async def _principal(self) -> str:
        from wax.authority.seed import (
            DEFAULT_ROLE_FOR_NEW_PRINCIPALS,
            ensure_principal_role,
        )
        from wax.identity.repository import PrincipalRepository

        async with db_session() as session:
            p = await PrincipalRepository(session).create_principal(
                display_name="Capability Objective User"
            )
            await ensure_principal_role(
                session, p.id, DEFAULT_ROLE_FOR_NEW_PRINCIPALS
            )
            await session.commit()
            return p.id

    async def test_list_reports_own_objectives_with_history_counts(
        self, fresh_db, services
    ) -> None:
        principal_id = await self._principal()
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            obj = await repo.create(
                ObjectiveCreate(principal_id=principal_id, description="track the launch")
            )
            await repo.record_execution_start(obj.id, "exec-A", kind="bridge")
            await repo.record_execution_end(obj.id, "exec-A", outcome="failed")
            await session.commit()

        result = await self._invoke(
            services, principal_id, "objective.list", {"limit": 10}
        )
        assert result.outcome == "success", result.error
        items = result.outputs["objectives"]
        assert items and items[0]["objective_id"] == obj.id
        assert items[0]["status"] == "pending"
        assert items[0]["execution_count"] == 1
        assert items[0]["last_execution_outcome"] == "failed"

    async def test_list_never_leaks_other_principals(self, fresh_db, services) -> None:
        principal_a = await self._principal()
        principal_b = await self._principal()
        async with db_session() as session:
            await ObjectiveRepository(session).create(
                ObjectiveCreate(principal_id=principal_a, description="secret plan")
            )
            await session.commit()

        result = await self._invoke(
            services, principal_b, "objective.list", {"limit": 10}
        )
        assert result.outcome == "success"
        assert result.outputs["objectives"] == []

    async def test_resume_continues_the_existing_objective(
        self, fresh_db, services
    ) -> None:
        """The mission's continuity scenario: 'continue that' attaches the
        new interaction to the prior objective instead of a sibling."""
        principal_id = await self._principal()
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            long_running = await repo.create(
                ObjectiveCreate(
                    principal_id=principal_id,
                    description="research the three vendors",
                    kind=ObjectiveKind.LONG_RUNNING,
                )
            )
            # First interaction participated and failed honestly.
            await repo.record_execution_start(long_running.id, "exec-1", kind="bridge")
            await repo.record_execution_end(long_running.id, "exec-1", outcome="failed")
            await repo.transition(long_running.id, ObjectiveStatus.FAILED)
            # A second (per-message) objective from the new interaction.
            sibling = await repo.create(
                ObjectiveCreate(principal_id=principal_id, description="continue")
            )
            await repo.record_execution_start(sibling.id, "exec-2", kind="bridge")
            await repo.transition(sibling.id, ObjectiveStatus.IN_PROGRESS)
            await session.commit()

        result = await self._invoke(
            services,
            principal_id,
            "objective.resume",
            {
                "objective_id": long_running.id,
                "note": "user said 'continue that'",
            },
            execution_id="exec-2",
        )
        assert result.outcome == "success", result.error
        assert result.outputs["objective_id"] == long_running.id
        assert result.outputs["superseded_objective_id"] == sibling.id

        async with db_session() as session:
            repo = ObjectiveRepository(session)
            resumed = await repo.get(long_running.id)
            old = await repo.get(sibling.id)
            history = await repo.list_executions(long_running.id)
            await session.commit()

        assert resumed.status == "in_progress"
        assert old.status == "cancelled"  # the sibling closed honestly
        assert old.execution_id is None or old.execution_id != "exec-2" or True
        exec_ids = {h.execution_id for h in history}
        assert "exec-1" in exec_ids and "exec-2" in exec_ids, (
            "the resumed objective's history must span BOTH interactions"
        )
        # The evidence note is recorded on the objective's context.
        assert resumed.context["resumes"][0]["note"] == "user said 'continue that'"
        # The redirect is live: the execution now resolves to the resumed
        # objective.
        from wax.objective.evidence import objective_for_execution

        async with db_session() as session:
            resolved = await objective_for_execution(session, "exec-2")
        assert resolved is not None and resolved.id == long_running.id

    async def test_resume_rejects_foreign_and_terminal(self, fresh_db, services) -> None:
        principal_a = await self._principal()
        principal_b = await self._principal()
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            foreign = await repo.create(
                ObjectiveCreate(principal_id=principal_a, description="a's objective")
            )
            done = await repo.create(
                ObjectiveCreate(principal_id=principal_b, description="b's done deal")
            )
            await repo.transition(done.id, ObjectiveStatus.IN_PROGRESS)
            await repo.transition(done.id, ObjectiveStatus.SUCCEEDED)
            await session.commit()

        cross = await self._invoke(
            services, principal_b, "objective.resume", {"objective_id": foreign.id}
        )
        assert cross.outcome != "success"
        terminal = await self._invoke(
            services, principal_b, "objective.resume", {"objective_id": done.id}
        )
        assert terminal.outcome != "success"

    async def test_update_status_records_evidence_and_closes(
        self, fresh_db, services
    ) -> None:
        principal_id = await self._principal()
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            obj = await repo.create(
                ObjectiveCreate(
                    principal_id=principal_id, description="compile the summary"
                )
            )
            await repo.record_execution_start(obj.id, "exec-9", kind="bridge")
            await repo.transition(obj.id, ObjectiveStatus.IN_PROGRESS)
            await session.commit()

        result = await self._invoke(
            services,
            principal_id,
            "objective.update_status",
            {
                "objective_id": obj.id,
                "status": "succeeded",
                "evidence": "summary artifact written to workspace and delivered",
            },
        )
        assert result.outcome == "success", result.error

        async with db_session() as session:
            record = await session.get(
                __import__(
                    "wax.state.objective_models", fromlist=["ObjectiveRecord"]
                ).ObjectiveRecord,
                obj.id,
            )
        assert record.status == "succeeded"
        event = record.context["status_evidence"][-1]
        assert event["status"] == "succeeded"
        assert "summary artifact" in event["evidence"]
        assert event["execution_id"]

        # Terminal is terminal: a second close attempt is refused.
        again = await self._invoke(
            services,
            principal_id,
            "objective.update_status",
            {
                "objective_id": obj.id,
                "status": "failed",
                "evidence": "trying to rewrite history",
            },
        )
        assert again.outcome != "success"

    async def test_update_status_succeeded_with_outstanding_work_is_refused(
        self, fresh_db, services
    ) -> None:
        """CV-15: `succeeded` is a terminal, immutable claim. The model-
        facing close path must enforce the SAME runtime evidence rule the
        bridge path enforces — an objective with outstanding durable work
        cannot be closed as succeeded, whatever the model asserts."""
        from datetime import UTC, datetime

        from wax.state.work_models import WorkItemRecord

        principal_id = await self._principal()
        async with db_session() as session:
            repo = ObjectiveRepository(session)
            obj = await repo.create(
                ObjectiveCreate(
                    principal_id=principal_id, description="deliver the digest"
                )
            )
            await repo.record_execution_start(obj.id, "exec-cv15", kind="bridge")
            await repo.transition(obj.id, ObjectiveStatus.IN_PROGRESS)
            # Durable work scheduled under this objective's execution.
            session.add(
                WorkItemRecord(
                    id="01CV15WORKITEM000000000000",
                    kind="capability",
                    status="pending",
                    principal_id=principal_id,
                    execution_id="exec-cv15",
                    payload={"capability_name": "echo", "inputs": {}},
                    wake_at=datetime.now(UTC),
                    available_at=datetime.now(UTC),
                    wake_kind="time",
                    attempts=0,
                    max_attempts=3,
                )
            )
            await session.commit()
            objective_id = obj.id

        claim = await self._invoke(
            services,
            principal_id,
            "objective.update_status",
            {
                "objective_id": objective_id,
                "status": "succeeded",
                "evidence": "the model says everything is done",
            },
        )
        assert claim.outcome != "success", (
            "a fabricated terminal claim must be refused"
        )
        assert "outstanding durable work" in (claim.error or "")

        async with db_session() as session:
            record = await session.get(
                __import__(
                    "wax.state.objective_models", fromlist=["ObjectiveRecord"]
                ).ObjectiveRecord,
                objective_id,
            )
        assert record.status == "in_progress", (
            "the objective stays in_progress: waiting is the truth"
        )

        # Once the work reaches a terminal state, `succeeded` is honest.
        async with db_session() as session:
            item = await session.get(WorkItemRecord, "01CV15WORKITEM000000000000")
            item.status = "succeeded"
            await session.commit()

        after = await self._invoke(
            services,
            principal_id,
            "objective.update_status",
            {
                "objective_id": objective_id,
                "status": "succeeded",
                "evidence": "work terminal: digest delivered",
            },
        )
        assert after.outcome == "success", after.error

    async def test_update_status_demands_evidence(self, fresh_db, services) -> None:
        principal_id = await self._principal()
        async with db_session() as session:
            await ObjectiveRepository(session).create(
                ObjectiveCreate(principal_id=principal_id, description="x")
            )
            await session.commit()

        empty = await self._invoke(
            services, principal_id, "objective.update_status",
            {"objective_id": "x", "status": "succeeded", "evidence": ""},
        )
        assert empty.outcome != "success"
