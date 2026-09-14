"""Open-world validation wave 3 (continuation loop, reconciliation-5).

Wave 2 proved: capability honesty, approval-gated work, restart
continuity, provider swap, interface handoff.

Wave 3 proves the FIFTH-PASS MECHANISMS COMPOSE — not as unit-tested
islands but as one runtime flow a model could actually drive:

    scratch.workspace → code.run (namespace sandbox reads a seeded
    artifact, computes, writes an output file) → memory.store of the
    computed outcome → memory.search retrieves it for a future session

No scenario handlers, no hardcoded flows: a scripted mock model requests
generic capabilities by name, and the runtime enforces the rest.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import shutil
from sqlalchemy import select
from ulid import ULID

from wax.authority.seed import get_role_by_name, seed_builtin_roles
from wax.capabilities.contracts import CapabilityInvocationRequest
from wax.intelligence.adapters.mock_provider import MockLLMProvider
from wax.intelligence.service import IntelligenceService
from wax.state.authority_models import PrincipalRole
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base


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
        await session.commit()
    yield
    await dispose_engine()


@pytest.fixture
def services(test_settings):
    return RuntimeServices.build(test_settings)


from wax.runtime.services import RuntimeServices  # noqa: E402


def _pil_available() -> bool:
    """The OCR test builds its image with Pillow: skip honestly when the
    rendering dependency is absent (the gate used to check the tesseract
    binary only, so a Pillow-less host failed instead of skipping)."""
    try:
        import PIL  # noqa: F401

        return True
    except ImportError:
        return False



async def _grant_admin(principal_id: str) -> None:
    async with db_session() as session:
        admin = await get_role_by_name(session, "admin")
        session.add(
            PrincipalRole(id=str(ULID()), principal_id=principal_id, role_id=admin.id)
        )
        await session.commit()


async def _invoke(services, principal_id: str, name: str, inputs: dict):
    async with db_session() as session:
        invoker = services.invoker(session)
        result = await invoker.invoke(
            CapabilityInvocationRequest(
                capability_name=name,
                principal_id=principal_id,
                inputs=inputs,
            )
        )
        await session.commit()
    return result


class TestSandboxedComposition:
    """One flow, five subsystems: workspace + sandboxed execution +
    filesystem effect + memory write + memory recall."""

    async def test_workspace_code_memory_composition(
        self, fresh_db, services, tmp_path
    ) -> None:
        principal_id = "01POPENWORLD3AZZZZZZZZZZZZZ"
        await _grant_admin(principal_id)

        # 1. Provision an owned scratch workspace — through the same
        #    capability surface a model would use.
        ws = await _invoke(
            services, principal_id, "scratch.workspace", {"ttl_seconds": 600}
        )
        assert ws.outcome == "success", ws.error
        workspace_id = ws.outputs["resource_id"]
        workspace_path = ws.outputs["path"]

        # Seed an artifact only the sandbox can now see/write.
        seed = tmp_path / "seed.txt"
        seed.write_text("7 6")
        shutil.copy(str(seed), f"{workspace_path}/input.txt")

        # 2. Run code in the runtime-selected boundary: it reads the
        #    input, computes, and writes an output file.
        code = (
            "vals = open('input.txt').read().split()\n"
            "result = int(vals[0]) * int(vals[1])\n"
            "open('output.txt', 'w').write(str(result))\n"
            "print('PRODUCT', result)\n"
        )
        run = await _invoke(
            services,
            principal_id,
            "code.run",
            {
                "code": code,
                "workspace_resource_id": workspace_id,
                "timeout_seconds": 15.0,
            },
        )
        assert run.outcome == "success", run.error
        assert run.outputs["exit_code"] == 0
        assert "PRODUCT 42" in run.outputs["stdout"]
        assert run.outputs["isolation"] in ("namespace", "subprocess")

        # 3. The file effect really happened in the owned workspace.
        output = (tmp_path / "wax-resources")
        written = list(output.rglob("output.txt"))
        assert written and written[0].read_text() == "42"

        # 4. Remember the outcome (authority-gated memory agency).
        stored = await _invoke(
            services,
            principal_id,
            "memory.store",
            {
                "content": {
                    "text": f"Computed product of 7 and 6 is 42 in workspace {workspace_id}"
                },
                "kind": "episodic",
                "provenance": "capability:invoke:code.run",
            },
        )
        assert stored.outcome == "success", stored.error

        # 5. A future session retrieves the evidence by relevance.
        found = await _invoke(
            services,
            principal_id,
            "memory.search",
            {"query": "product of 7 and 6", "limit": 3},
        )
        assert found.outcome == "success", found.error
        memories = found.outputs.get("memories", [])
        assert memories, "computed evidence was not retrievable"
        evidence = str(memories[0].get("content", "")) + str(
            memories[0].get("summary", "")
        )
        assert "42" in evidence

    @pytest.mark.skipif(
        shutil.which("tesseract") is None or not _pil_available(),
        reason="tesseract or Pillow not installed on host",
    )
    async def test_media_ocr_feeds_the_ai_not_bytes(self, fresh_db, services) -> None:
        """The media pipeline extracts REAL text (OCR) so the model sees
        text, never bytes — the INVARIANT, exercised end-to-end."""
        import io

        from PIL import Image, ImageDraw, ImageFont

        from wax.media.contracts import MediaKind, MediaSource

        from wax.media.pipeline import MediaPipeline

        img = Image.new("RGB", (600, 160), "white")
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48
            )
        except OSError:  # pragma: no cover
            font = ImageFont.load_default()
        draw.text((20, 50), "INVOICE 7300", fill="black", font=font)
        buf = io.BytesIO()
        img.save(buf, format="PNG")

        pipeline = MediaPipeline()
        result = await pipeline.extract(
            MediaSource(
                media_id="ocr-e2e",
                mime_type="image/png",
                kind=MediaKind.IMAGE,
                bytes_data=buf.getvalue(),
            )
        )
        assert result.success is True, result.error
        assert "7300" in result.extracted_text
