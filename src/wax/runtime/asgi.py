"""ASGI entrypoint for production servers.

Usage:
    uvicorn wax.runtime.asgi:app --host 0.0.0.0 --port 8000

For development, use:
    uvicorn wax.runtime.asgi:app --reload --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

from wax.runtime.app import create_app

# Module-level app instance for ASGI servers.
app = create_app()
