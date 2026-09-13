"""Permissions enum for WAX.

Permissions are capability-scoped strings following the format:
    <namespace>.<action>[:<resource>]

Examples:
    memory.read
    memory.write
    capability.invoke:any
    capability.invoke:web_search
    execution.start
    execution.cancel
    admin.role.assign
    admin.principal.delete

The wildcard "any" matches any resource within the action. A future
improvement may add a policy engine (e.g., OPA/cedar) but for now we use
simple string matching.
"""

from __future__ import annotations

from enum import StrEnum


class PermissionNamespace(StrEnum):
    """Top-level permission namespaces."""

    MEMORY = "memory"
    CAPABILITY = "capability"
    EXECUTION = "execution"
    IDENTITY = "identity"
    AUTHORITY = "authority"
    AUDIT = "audit"
    ADMIN = "admin"


# Built-in permissions. The MANIFEST below is the single source of
# truth; PermissionNamespace groups it and BUILTIN_PERMISSIONS derives
# from it, so an enum member and a permission string cannot drift apart.
# New permissions are added as capabilities are introduced. Adding a
# permission is a deliberate architectural act.
_PERMISSION_MANIFEST: tuple[str, ...] = (
    # Memory (Phase F)
    "memory.read",
    "memory.write",
    # Capabilities (Phase G)
    "capability.invoke:any",
    "capability.invoke:built_in",  # built-in tools (http.get, http.post)
    "capability.invoke:web_search",
    "capability.invoke:code_run",
    # Execution (Phase H)
    "execution.start",
    "execution.cancel",
    "execution.read",
    # Identity (Phase D)
    "identity.read",
    "identity.create",
    "identity.delete",
    # Authority (Phase E)
    "authority.role.assign",
    "authority.role.revoke",
    "authority.role.create",
    # Audit (Phase C)
    "audit.read",
    # Admin
    "admin.*",
)

BUILTIN_PERMISSIONS: frozenset[str] = frozenset(_PERMISSION_MANIFEST)


def _assert_manifest_namespaces() -> None:
    """Import-time consistency: every manifest permission's namespace
    must be a declared PermissionNamespace (drift fails fast)."""
    declared = {ns.value for ns in PermissionNamespace}
    for permission in _PERMISSION_MANIFEST:
        namespace = permission.split(".", 1)[0]
        assert namespace in declared, (
            f"permission {permission!r} uses undeclared namespace "
            f"{namespace!r} (add it to PermissionNamespace)"
        )


_assert_manifest_namespaces()


# Built-in roles
BUILTIN_ROLES: dict[str, frozenset[str]] = {
    "admin": frozenset(
        {
            "memory.read",
            "memory.write",
            "capability.invoke:any",
            "execution.start",
            "execution.cancel",
            "execution.read",
            "identity.read",
            "identity.create",
            "identity.delete",
            "authority.role.assign",
            "authority.role.revoke",
            "authority.role.create",
            "audit.read",
            "admin.*",
        }
    ),
    "member": frozenset(
        {
            "memory.read",
            "memory.write",
            "capability.invoke:built_in",
            "execution.start",
            "execution.read",
        }
    ),
    "service": frozenset(
        {
            "memory.read",
            "memory.write",
            "audit.read",
        }
    ),
    "ai": frozenset(
        {
            # The AI is always an untrusted requester. It has NO permissions
            # of its own. Permissions are granted on behalf of a principal,
            # and the runtime enforces the principal's permissions, never
            # the AI's. (Directive §7, §147)
            #
            # This is intentionally empty.
        }
    ),
}


def is_permission_granted(
    granted_permissions: frozenset[str], required: str
) -> bool:
    """Check whether `required` permission is satisfied by any in `granted_permissions`.

    A granted permission matches `required` if:
    - they are equal, OR
    - the granted permission is "<ns>.<action>:any" and required is "<ns>.<action>:<x>", OR
    - the granted permission is "<ns>.*" and required starts with "<ns>.".

    Examples:
        granted={"memory.read"}, required="memory.read" → True
        granted={"capability.invoke:any"}, required="capability.invoke:web_search" → True
        granted={"admin.*"}, required="admin.role.assign" → True
        granted={"memory.write"}, required="memory.read" → False
    """
    if required in granted_permissions:
        return True

    # Check for wildcards
    parts = required.split(":")
    if len(parts) == 2:
        ns_action, _resource = parts
        if f"{ns_action}:any" in granted_permissions:
            return True

    # Check for namespace-level wildcards
    if "." in required:
        ns = required.split(".", 1)[0]
        if f"{ns}.*" in granted_permissions:
            return True

    return False
