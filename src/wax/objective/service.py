"""Objective service — the runtime's API for objective lifecycle.

The service composes:
- ObjectiveRepository (persistence)
- AuthorizationService (runtime-enforced — only the principal who owns
  an objective may transition it; the AI cannot transition objectives
  on its own behalf without the principal's authority)
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from wax.authority.service import AuthorizationService
from wax.core.exceptions import WaxNotFoundError, WaxPermissionDeniedError
from wax.objective.contracts import ObjectiveCreate, ObjectiveStatus
from wax.objective.repository import ObjectiveRepository
from wax.state.objective_models import ObjectiveRecord


class ObjectiveService:
    """High-level objective operations.

    All operations require authorization. The AI may not create, transition,
    or list objectives on its own — it must act on behalf of an authorized
    principal.
    """

    def __init__(
        self,
        session: AsyncSession,
        auth: AuthorizationService,
    ) -> None:
        self._session = session
        self._auth = auth
        self._repo = ObjectiveRepository(session)

    async def create_for_principal(
        self,
        principal_id: str,
        payload: ObjectiveCreate,
    ) -> ObjectiveRecord:
        """Create an objective on behalf of a principal.

        The principal_id in the payload MUST match the authenticated
        principal (the AI cannot create objectives for someone else).
        """
        if payload.principal_id != principal_id:
            raise WaxPermissionDeniedError("Cannot create objective for a different principal")
        return await self._repo.create(payload)

    async def get_for_principal(self, principal_id: str, objective_id: str) -> ObjectiveRecord:
        """Get an objective, ensuring it belongs to the principal."""
        record = await self._repo.get(objective_id)
        if record is None:
            raise WaxNotFoundError(f"Objective not found: {objective_id}")
        if record.principal_id != principal_id:
            # Do not leak existence to unauthorized principals.
            raise WaxNotFoundError(f"Objective not found: {objective_id}")
        return record

    async def list_for_principal(
        self, principal_id: str, **kwargs: object
    ) -> list[ObjectiveRecord]:
        return await self._repo.list_for_principal(principal_id, **kwargs)  # type: ignore[arg-type]

    async def transition(
        self,
        principal_id: str,
        objective_id: str,
        new_status: ObjectiveStatus | str,
    ) -> ObjectiveRecord:
        # Verify ownership before transition
        await self.get_for_principal(principal_id, objective_id)
        ok = await self._repo.transition(objective_id, new_status)
        if not ok:
            raise WaxPermissionDeniedError(
                f"Cannot transition objective {objective_id} to {new_status}"
            )
        return await self.get_for_principal(principal_id, objective_id)
