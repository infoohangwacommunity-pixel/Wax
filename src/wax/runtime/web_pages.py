"""Temporary web pages — ephemeral HTTP endpoints (Parts 16-17).

Instead of building dashboards, the AI creates pages only when needed.
The AI generates a page, starts a local server, exposes the URL, the
student opens it, submits, and the AI continues. The page disappears.

Examples:
- GitHub login / OAuth callback
- File upload (WAEC result, assignment)
- Secret input (API keys)
- Payment confirmation
- Survey / form

The AI creates these through the terminal:
1. Write an HTML file to its workspace
2. Start a detached HTTP server (python -m http.server)
3. The runtime exposes the URL to the student
4. When the student submits, the AI reads the result from a file
5. The page expires (server killed, files cleaned)

This module provides a helper the AI can use from its terminal:
    import wax_runtime; wax_runtime.serve_page("upload.html", port=8080)

The runtime tracks active pages and cleans them up when the execution ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from wax.runtime.logging import get_logger

log = get_logger(__name__)


@dataclass
class ServedPage:
    """A temporary web page the AI created."""

    page_id: str
    url: str
    port: int
    html_path: str
    started_at: datetime
    process: asyncio.subprocess.Process | None = None
    purpose: str = ""  # what the page is for (OAuth, upload, etc.)


@dataclass
class WebPageServer:
    """Manages temporary web pages for one execution.

    The AI creates pages through the terminal. The runtime tracks them
    and cleans them up when the execution ends.
    """

    workspace: Path
    _pages: list[ServedPage] = field(default_factory=list)

    def find_free_port(self) -> int:
        """Find a free TCP port for a temporary server."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    async def serve_page(
        self,
        html_content: str,
        *,
        purpose: str = "",
        port: int | None = None,
        host: str = "0.0.0.0",
    ) -> ServedPage:
        """Serve a temporary HTML page.

        Writes the HTML to a file in the workspace, starts a detached
        HTTP server, and returns the URL. The AI sends the URL to the
        student. When the student submits, the AI reads the result.

        The page is cleaned up when `cleanup()` is called (at execution end).
        """
        if port is None:
            port = self.find_free_port()

        page_id = str(uuid.uuid4())[:8]
        html_path = self.workspace / f"page_{page_id}.html"
        html_path.write_text(html_content)

        # Start a simple HTTP server in the workspace directory
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "-c",
            f"cd {self.workspace} && python3 -m http.server {port} --bind {host}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

        # Determine the external URL (in production this would be the
        # public hostname; in dev it's localhost)
        url = f"http://{host}:{port}/page_{page_id}.html"

        page = ServedPage(
            page_id=page_id,
            url=url,
            port=port,
            html_path=str(html_path),
            started_at=datetime.utcnow(),
            process=proc,
            purpose=purpose,
        )
        self._pages.append(page)

        log.info(
            "web_page.served",
            page_id=page_id,
            url=url,
            purpose=purpose,
        )

        return page

    async def cleanup(self) -> None:
        """Kill all temporary servers and clean up HTML files."""
        import os
        import signal

        for page in self._pages:
            if page.process and page.process.returncode is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(os.getpgid(page.process.pid), signal.SIGTERM)
            # Remove the HTML file
            with contextlib.suppress(FileNotFoundError):
                Path(page.html_path).unlink()

        self._pages.clear()
        log.info("web_page.all_cleaned")

    def active_pages(self) -> list[ServedPage]:
        """Return currently active temporary pages."""
        return list(self._pages)


# ---------------------------------------------------------------------------
# Convenience HTML generators — the AI can call these from the terminal
# or write its own HTML. These are NOT capabilities; they're convenience.
# ---------------------------------------------------------------------------


def upload_page_html(*, title: str = "Upload File", action: str = "/upload") -> str:
    """Generate a simple file upload page."""
    return f"""<!DOCTYPE html>
<html>
<head><title>{title}</title></head>
<body>
<h1>{title}</h1>
<form action="{action}" method="POST" enctype="multipart/form-data">
  <input type="file" name="file" required>
  <button type="submit">Upload</button>
</form>
</body>
</html>"""


def secret_input_html(*, title: str = "Enter Secret", label: str = "Secret") -> str:
    """Generate a secret input page (for API keys, tokens, etc.)."""
    return f"""<!DOCTYPE html>
<html>
<head><title>{title}</title></head>
<body>
<h1>{title}</h1>
<form action="/submit" method="POST">
  <label>{label}:</label>
  <input type="password" name="secret" required>
  <button type="submit">Submit</button>
</form>
</body>
</html>"""


def oauth_callback_html(*, service: str = "Service") -> str:
    """Generate an OAuth callback page."""
    return f"""<!DOCTYPE html>
<html>
<head><title>{service} Login</title></head>
<body>
<h1>Connecting to {service}</h1>
<p>Complete the login in the popup. This page will close automatically.</p>
<script>
  window.onload = function() {{
    // The AI's server handles the OAuth callback
    setTimeout(function() {{ window.close(); }}, 3000);
  }};
</script>
</body>
</html>"""
