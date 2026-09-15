"""INV-02 enforcement — core WAX must not depend on any specific interface.

This test EXISTS since the constitutional audit (2026-09-14). Before that,
core/invariants.py INV-02 claimed enforcement by this file while the file
did not exist — a phantom enforcement mechanism, itself a violation.

The rule (AST import scan, no runtime imports):
- Only the interface boundary may import the interface layer:
    wax.interfaces.**            (the adapters themselves)
    wax.runtime.bridge.**        (the interface bridge)
    wax.runtime.app              (the composition root that wires adapters)
- Nothing else in src/wax may import those modules. A new import there
  fails this test — WhatsApp concepts cannot leak upward silently.

String mentions (config field names, credential kinds) are deliberately
NOT violations: configuration must name what it configures, and the
identity boundary table must name credential kinds. Coupling is imports.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "wax"

# Modules allowed to import the interface boundary (adapters + bridge +
# composition root). Everything else in src/wax is runtime core.
INTERFACE_BOUNDARY_PREFIXES = ("wax.interfaces", "wax.runtime.bridge")
_ALLOWED_IMPORTERS = (
    "wax.interfaces",
    "wax.runtime.bridge",
    "wax.runtime.app",  # composition root: wires adapters into the runtime
)


def _iter_py_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC.parent)
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imported_modules(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append(node.module)
    return found


def _is_boundary_module(module: str) -> bool:
    return module == "wax.runtime.app" or module.startswith("wax.runtime.app.")


def test_no_interface_coupling_outside_the_boundary() -> None:
    violations: list[str] = []
    for path in _iter_py_files():
        module = _module_name(path)
        if not module or module.startswith(_ALLOWED_IMPORTERS):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for imported in _imported_modules(tree):
            if imported.startswith(INTERFACE_BOUNDARY_PREFIXES):
                violations.append(f"{module} imports {imported}")
    assert not violations, (
        "INV-02 violation — runtime core imports the interface boundary:\n" + "\n".join(violations)
    )


def test_composition_root_is_the_only_runtime_wiring_point() -> None:
    """The bridge must be wired only from app.py — no other runtime module
    may import it even indirectly from within runtime/."""
    runtime_dir = SRC / "runtime"
    violations: list[str] = []
    for path in sorted(runtime_dir.rglob("*.py")):
        module = _module_name(path)
        if not module or module.startswith(("wax.runtime.app", "wax.runtime.bridge")):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for imported in _imported_modules(tree):
            if imported.startswith("wax.runtime.bridge"):
                violations.append(f"{module} imports {imported}")
    assert not violations, "INV-02 violation — runtime plumbing imports the bridge:\n" + "\n".join(
        violations
    )
