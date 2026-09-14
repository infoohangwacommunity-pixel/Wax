"""INV-03 enforcement — core WAX must not depend on any specific model provider.

This test EXISTS since the constitutional audit (2026-09-14). Before that,
core/invariants.py INV-03 claimed enforcement by this file while the file
did not exist — a phantom enforcement mechanism, itself a violation.

The rule (AST import scan, no runtime imports):
1. Third-party provider SDKs (openai, anthropic, tiktoken) may ONLY be
   imported inside wax/intelligence/adapters/ — the replaceable boundary.
2. The adapter package may only be imported by the adapters themselves and
   by wax.intelligence.service — the provider FACTORY that turns settings
   into providers. No other module may reach past the factory.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "wax"

PROVIDER_SDKS = ("openai", "anthropic", "tiktoken")
ADAPTER_PACKAGE = "wax.intelligence.adapters"
ADAPTER_IMPORTERS_ALLOWED = (
    "wax.intelligence.adapters",  # the adapters themselves
    "wax.intelligence.service",  # the provider factory (settings -> provider)
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


def test_provider_sdks_live_only_in_adapters() -> None:
    violations: list[str] = []
    for path in _iter_py_files():
        module = _module_name(path)
        rel = path.relative_to(SRC)
        inside_adapters = rel.parts[:2] == ("intelligence", "adapters") or rel.parts[
            :1
        ] == ("intelligence",) and rel.name.startswith("adapters")
        if inside_adapters:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for imported in _imported_modules(tree):
            root = imported.split(".")[0]
            if root in PROVIDER_SDKS:
                violations.append(f"{module} imports {imported}")
    assert not violations, (
        "INV-03 violation — provider SDK imported outside the adapter "
        "boundary:\n" + "\n".join(violations)
    )


def test_adapter_package_reachable_only_through_the_factory() -> None:
    violations: list[str] = []
    for path in _iter_py_files():
        module = _module_name(path)
        if not module or module.startswith(ADAPTER_IMPORTERS_ALLOWED):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for imported in _imported_modules(tree):
            if imported.startswith(ADAPTER_PACKAGE):
                violations.append(f"{module} imports {imported}")
    assert not violations, (
        "INV-03 violation — modules bypassing the provider factory to reach "
        "adapters directly:\n" + "\n".join(violations)
    )
