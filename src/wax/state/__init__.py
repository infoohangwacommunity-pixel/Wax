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
