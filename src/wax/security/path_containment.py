"""Path containment utility (P0-7).

Central utility for resolving workspace-relative paths safely.
Rejects: absolute paths, .. traversal, symlinks.
Used by all workspace/artifact capabilities to prevent path escape.
"""

from __future__ import annotations

from pathlib import Path

from wax.runtime.logging import get_logger

log = get_logger(__name__)


class PathContainmentError(ValueError):
    """Raised when a path escapes the workspace boundary."""


def resolve_workspace_path(
    workspace_root: str | Path,
    relative_path: str,
    *,
    follow_symlinks: bool = False,
    must_exist: bool = False,
) -> Path:
    """Resolve a workspace-relative path safely.

    Rejects:
    - Absolute paths (starting with /)
    - Paths containing .. (traversal)
    - Symlinks that point outside the workspace
    - Paths that resolve outside the workspace root

    Returns the resolved absolute Path inside the workspace.

    Args:
        workspace_root: The workspace's absolute root path.
        relative_path: The workspace-relative path to resolve.
        follow_symlinks: If True, allow symlinks (default False for safety).
        must_exist: If True, raise if the file doesn't exist.
    """
    if not relative_path:
        raise PathContainmentError("path must not be empty")

    # Reject absolute paths
    if relative_path.startswith("/"):
        raise PathContainmentError(
            f"absolute paths are not allowed: {relative_path}"
        )

    # Reject .. traversal
    parts = Path(relative_path).parts
    if ".." in parts:
        raise PathContainmentError(
            f"path traversal is not allowed: {relative_path}"
        )

    root = Path(workspace_root).resolve()
    target = (root / relative_path).resolve()

    # Verify the resolved path is inside the workspace root
    if not str(target).startswith(str(root)):
        raise PathContainmentError(
            f"path escapes the workspace: {relative_path} -> {target}"
        )

    # Reject symlinks (unless explicitly allowed)
    if not follow_symlinks and target.is_symlink():
        raise PathContainmentError(
            f"symlinks are not allowed: {relative_path}"
        )

    # Check existence if required
    if must_exist and not target.exists():
        raise PathContainmentError(
            f"file does not exist: {relative_path}"
        )

    return target
