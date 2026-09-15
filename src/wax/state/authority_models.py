"""wax.state.authority_models — persistence for the authorization system.

Tables:
- roles: named bundles of permissions (admin, member, service, ai)
- principal_roles: which principals have which roles

Note: permissions are stored as a JSON array on each role. The set of
permissions grows as capabilities are added; the source of truth is
`wax.authority.permissions.BUILTIN_PERMISSIONS`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class Role(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A named bundle of permissions assignable to principals.

    Permissions are stored as a JSON array of strings. The source of truth
    for what permissions exist is `wax.authority.permissions.BUILTIN_PERMISSIONS`.
    """

    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("name", name="uq_roles_name"),)

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # JSON array of permission strings, e.g. ["memory.read", "memory.write"]
    # `default=list` fires at INSERT; we additionally initialize in
    # `__init__` so the attribute is a list at construction time.
    permissions: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )

    def __init__(self, **kwargs: Any) -> None:
        # Ensure permissions is a list from the moment the object is built,
        # so callers can `role.permissions.append(...)` immediately.
        if "permissions" not in kwargs or kwargs["permissions"] is None:
            kwargs["permissions"] = []
        super().__init__(**kwargs)

    def add_permission(self, permission: str) -> None:
        """Add a permission to this role. No-op if already present."""
        if permission not in self.permissions:
            self.permissions.append(permission)

    def remove_permission(self, permission: str) -> None:
        """Remove a permission from this role. No-op if not present."""
        if permission in self.permissions:
            self.permissions.remove(permission)


class PrincipalRole(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A role assigned to a principal. Grants all the role's permissions."""

    __tablename__ = "principal_roles"
    __table_args__ = (UniqueConstraint("principal_id", "role_id", name="uq_principal_roles"),)

    principal_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("principals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
