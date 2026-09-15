"""Context sections: active work, artifacts, environment (ADR-0023).

Mission Phase 5: the context the intelligence receives must cover the
semantic sections of its current situation — not only objective,
conversation, and memory. §99 requires a resumed objective to see its
pending actions; §56/§100 require artifacts to be runtime state distinct
from memory; §111 requires that under a tiny budget the objective and
critical state survive while lower-priority material is dropped.

Exercised through the REAL capability chain (workspace.acquire records
the artifact) and the REAL continuity assembly.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import httpx
import pytest

from wax.authority.seed import ensure_principal_role, seed_builtin_roles
from wax.capabilities.contracts import CapabilityInvocationRequest
from wax.continuity.assembly import (
    PRIORITY_ACTIVE_WORK,
    PRIORITY_ARTIFACTS,
    PRIORITY_ENVIRONMENT,
    PRIORITY_MEMORY,
    PRIORITY_OBJECTIVE,
    assemble_evidence,
    build_evidence_sections,
)
from wax.continuity.contracts import ContinuityContext
from wax.continuity.service import ContinuityService
from wax.runtime.provisioning import ProvisioningService
from wax.runtime.services import RuntimeServices
from wax.state.artifact_models import ArtifactRecord
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base

TEST_PRINCIPAL = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
ALLOWED_HOST = "files.pythonhosted.org"
CONTENT = b"artifact-context-section-test-bytes"
CONTENT_SHA = hashlib.sha256(CONTENT).hexdigest()


@pytest.fixture
async def fresh_db(test_settings, tmp_path):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    test_settings.__dict__["provisioning_root"] = str(tmp_path / "wax-resources")
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db_session() as session:
        await seed_builtin_roles(session)
        await ensure_principal_role(session, TEST_PRINCIPAL)
        await session.commit()
    yield
    await dispose_engine()


@pytest.fixture
def services(test_settings) -> RuntimeServices:
    return RuntimeServices.build(test_settings)


def _mock_transport(body: bytes):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    return httpx.MockTransport(handler)


async def _acquire_capability(services: RuntimeServices) -> str:
    """Run workspace.acquire through the REAL invoker; return artifact id."""
    services.acquisition_transport = _mock_transport(CONTENT)
    async with db_session() as session:
        resource = await ProvisioningService(services.settings).provision_scratch_dir(
            session, principal_id=TEST_PRINCIPAL, ttl_seconds=900
        )
        await session.commit()
        resource_id = resource.id

    async with db_session() as session:
        invoker = services.invoker(session)
        result = await invoker.invoke(
            CapabilityInvocationRequest(
                capability_name="workspace.acquire",
                principal_id=TEST_PRINCIPAL,
                inputs={
                    "workspace_resource_id": resource_id,
                    "url": f"https://{ALLOWED_HOST}/ctx-1.0.tar.gz",
                    "sha256": CONTENT_SHA,
                    "filename": "ctx-1.0.tar.gz",
                },
                request_id=f"exec-{TEST_PRINCIPAL[:8]}",
            )
        )
        await session.commit()
    assert result.outcome == "success", result.error
    return result.outputs["artifact_id"]


class TestArtifactRecords:
    async def test_acquisition_creates_first_class_record(self, fresh_db, services) -> None:

        artifact_id = await _acquire_capability(services)

        async with db_session() as session:
            record = await session.get(ArtifactRecord, artifact_id)
            await session.commit()

        assert record is not None
        assert record.principal_id == TEST_PRINCIPAL
        assert record.filename == "ctx-1.0.tar.gz"
        assert record.sha256 == CONTENT_SHA
        assert record.size_bytes == len(CONTENT)
        assert record.source == "workspace.acquire"
        assert record.workspace_resource_id
        assert record.expires_at is not None, "the artifact TTL must mirror its workspace's"
        # No absolute host path leakage: the record's path is
        # workspace-relative (mission §56: not arbitrary host paths).
        assert not record.path.startswith("/")

    async def test_artifacts_are_principal_scoped(self, fresh_db, services) -> None:
        await _acquire_capability(services)
        async with db_session() as session:
            from sqlalchemy import select

            rows = (
                (
                    await session.execute(
                        select(ArtifactRecord).where(
                            ArtifactRecord.principal_id == "01OTHERPRINCIPAL0000000"
                        )
                    )
                )
                .scalars()
                .all()
            )
            await session.commit()
        assert rows == []


class TestContextSections:
    async def test_build_context_carries_work_artifacts_environment(
        self, fresh_db, services
    ) -> None:
        """Acquire an artifact + schedule work, then build context: the
        ACTIVE_WORK, ARTIFACTS, and ENVIRONMENT evidence must be present
        (mission §99: a resumed objective reconstructs pending actions
        and artifacts)."""
        artifact_id = await _acquire_capability(services)

        # Outstanding durable work for the same principal.
        from wax.runtime.work.repository import WorkRepository

        async with db_session() as session:
            item = await WorkRepository(session).schedule(
                kind="capability",
                payload={"capability_name": "echo", "inputs": {"m": "x"}},
                wake_at=datetime.now(UTC),
                principal_id=TEST_PRINCIPAL,
                execution_id=None,
                max_attempts=3,
                wake_kind="time",
                wake_event=None,
                expires_at=None,
            )
            await session.commit()
            work_id = item.id

        async with db_session() as session:
            continuity = ContinuityService(session)
            context, _conversation_id = await continuity.build_context(
                TEST_PRINCIPAL, "whatsapp", "what is the status?"
            )
            await session.commit()

        assert any(w["work_id"] == work_id for w in context.active_work), (
            "outstanding work must surface as ACTIVE_WORK evidence"
        )
        assert any(a["artifact_id"] == artifact_id for a in context.recent_artifacts), (
            "the acquired artifact must surface as ARTIFACTS evidence"
        )
        assert context.environment.get("interface") == "whatsapp"

    async def test_sections_render_in_mission_priority_order(self) -> None:
        context = ContinuityContext(
            principal_id="p",
            is_new_conversation=False,
            days_since_last_message=0.5,
            conversation_summary="earlier discussion of the report",
            active_objective_description="finish the report",
            active_work=[
                {
                    "work_id": "w1",
                    "status": "pending",
                    "wake_kind": "time",
                    "wake_at": datetime.now(UTC).isoformat(),
                    "attempts": 0,
                    "capability": "message.send",
                }
            ],
            recent_memories=[{"summary": "user prefers markdown", "reason": "relevant"}],
            recent_artifacts=[
                {
                    "artifact_id": "a1",
                    "filename": "report.pdf",
                    "sha256": "abc123",
                    "bytes": 4567,
                    "source": "workspace.acquire",
                }
            ],
            environment={"interface": "whatsapp"},
        )
        sections = build_evidence_sections(context)
        by_kind = {s.kind: s for s in sections}
        assert set(by_kind) >= {
            "objective",
            "active_work",
            "memory",
            "conversation",
            "artifacts",
            "environment",
        }
        order = [s.kind for s in sorted(sections, key=lambda s: s.priority)]
        assert order.index("objective") < order.index("active_work")
        assert order.index("active_work") < order.index("memory")
        assert order.index("memory") < order.index("artifacts")
        assert order.index("artifacts") < order.index("environment")
        # Priorities carry the mission's hierarchy as constants.
        assert (
            PRIORITY_OBJECTIVE
            < PRIORITY_ACTIVE_WORK
            < PRIORITY_MEMORY
            < PRIORITY_ARTIFACTS
            < PRIORITY_ENVIRONMENT
        )

    async def test_degraded_budget_keeps_objective_and_active_work(
        self, fresh_db, services
    ) -> None:
        """Mission §111: forced tiny budget — objective and critical
        active-work state survive; artifacts/environment are dropped."""
        context = ContinuityContext(
            principal_id="p",
            active_objective_description="finish the quarterly report",
            active_work=[
                {
                    "work_id": "w1",
                    "status": "pending",
                    "wake_kind": "time",
                    "wake_at": datetime.now(UTC).isoformat(),
                    "attempts": 0,
                    "capability": "message.send",
                }
            ],
            recent_memories=[{"summary": "misc context " * 20, "reason": "relevant"}],
            recent_artifacts=[
                {
                    "artifact_id": "a1",
                    "filename": "big.bin",
                    "sha256": "abc123",
                    "bytes": 999,
                    "source": "workspace.acquire",
                }
            ],
            environment={"interface": "whatsapp"},
        )
        sections = build_evidence_sections(context)
        kept = assemble_evidence(sections, budget_chars=250)
        joined = "\n".join(kept)

        assert "[evidence: objective]" in joined
        assert "[evidence: active_work]" in joined
        assert "[evidence: artifacts]" not in joined
        assert "truncated" in joined, "cuts must be announced"
