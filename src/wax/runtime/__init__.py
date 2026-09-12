"""wax.runtime — the WAX runtime process.

This package contains the running WAX process: the FastAPI application,
lifecycle management, structured logging, and the HTTP surface that adapters
(WhatsApp, Web, future interfaces) connect into.

This package is allowed to do I/O. It must not be imported by `wax.core`.
"""
