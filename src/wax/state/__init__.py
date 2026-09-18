"""wax.state — persistence layer for WAX.

This package contains:
- Database engine + session management (async SQLAlchemy)
- ORM models (SQLAlchemy 2.0 declarative)
- Repositories (data access objects)
- Migrations (via Alembic)

Architectural rules:
- Models live here, NOT in wax.core. Core defines contracts (Pydantic);
  state defines persistence (SQLAlchemy).
- Repository pattern: callers go through repository classes, never touch
  the Session directly. This isolates the ORM choice.
- Migrations must be reversible; each must have upgrade + downgrade.
"""

from wax.state.engine import db_session, dispose_engine, get_engine, init_engine
from wax.state.models import Base

__all__ = [
    "Base",
    "db_session",
    "dispose_engine",
    "get_engine",
    "init_engine",
]

# Import every model module so Base.metadata is complete for ANY entry
# point that imports wax.state (tests create_all, alembic autogenerate,
# future tooling). Adding a model module here is mandatory.
from wax.state import (  # noqa: F401
    audit_models,
    bridge_models,
    context_models,
    continuity_models,
    delivery_models,
    execution_models,
    identity_models,
    interaction_models,
    memory_models,
    process_models,
    work_models,
)
