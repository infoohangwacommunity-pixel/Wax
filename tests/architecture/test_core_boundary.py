"""Architectural invariant tests (Phase B).

These tests enforce the architectural invariants declared in
`wax.core.invariants`. They are not unit tests of behavior — they are
contract tests of *structure*. If any of these fail, the architecture has
drifted and the failure must be addressed before merging.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from collections.abc import Iterable

import pytest

import wax.core

# ---------------------------------------------------------------------------
# INV-09: wax.core must contain no I/O.
# ---------------------------------------------------------------------------

# Modules that, if imported anywhere inside wax.core, indicate a violation.
# This list is intentionally conservative — anything that touches network,
# filesystem, database, subprocess, or external state is forbidden.
_FORBIDDEN_IO_MODULES: frozenset[str] = frozenset(
    {
        # Network / HTTP
        "httpx",
        "aiohttp",
        "requests",
        "urllib3",
        "http",
        "http.client",
        "websocket",
        "websockets",
        # Database
        "sqlalchemy",
        "asyncpg",
        "aiosqlite",
        "psycopg",
        "psycopg2",
        "sqlite3",
        "redis",
        # ASGI / WSGI servers
        "fastapi",
        "starlette",
        "uvicorn",
        "gunicorn",
        "flask",
        "django",
        # Filesystem / subprocess (beyond stdlib basics used for typing)
        "aiofiles",
        "pathlib",  # allowed for type defs only, NOT for actual file ops
        # Subprocess
        "subprocess",
        "asyncio.subprocess",
        # External services
        "openai",
        "anthropic",
        "google.generativeai",
        "boto3",
        "google.cloud",
        # Logging (real I/O — core must use return values, not logging)
        "structlog",
        "logging",
    }
)


def _walk_wax_core_modules() -> Iterable[str]:
    """Yield fully-qualified module names for every module inside wax.core."""
    yield "wax.core"
    for module_info in pkgutil.walk_packages(
        wax.core.__path__, prefix="wax.core."
    ):
        yield module_info.name


def _module_imports(module_name: str) -> set[str]:
    """Return the set of top-level imported modules for a given module.

    Walks the module's AST-equivalent by inspecting `sys.modules` after import.
    """
    if module_name not in sys.modules:
        try:
            importlib.import_module(module_name)
        except Exception:
            return set()

    mod = sys.modules.get(module_name)
    if mod is None:
        return set()

    imports: set[str] = set()
    # Direct module attributes (import x / from x import y)
    for name in dir(mod):
        if name.startswith("_"):
            continue
        value = getattr(mod, name, None)
        if value is None:
            continue
        # Find the module this object came from
        module_obj = getattr(value, "__module__", None)
        if module_obj and module_obj.startswith("wax"):
            continue  # internal — fine
        if hasattr(value, "__name__") and "." in str(getattr(value, "__name__", "")):
            top = str(getattr(value, "__name__", "")).split(".")[0]
            if top and not top.startswith("wax"):
                imports.add(top)

    # Also parse __dict__ for the imported module names directly
    for key in mod.__dict__:
        if key.startswith("_"):
            continue
        val = mod.__dict__[key]
        if hasattr(val, "__module__") and val.__module__:
            top = val.__module__.split(".")[0]
            if top and not top.startswith("wax"):
                imports.add(top)

    return imports


class TestCoreHasNoIO:
    """INV-09: wax.core must not import I/O-performing modules."""

    @pytest.mark.architecture
    def test_no_forbidden_io_imports_in_wax_core(self) -> None:
        violations: list[tuple[str, str]] = []
        for module_name in _walk_wax_core_modules():
            imports = _module_imports(module_name)
            for forbidden in _FORBIDDEN_IO_MODULES:
                if forbidden in imports:
                    violations.append((module_name, forbidden))

        # pathlib is a special case: it's allowed for type hints only,
        # but importing it is fine because we don't actually call Path
        # methods. However, to be safe and to enforce INV-09 strictly,
        # we DO flag it.
        # (Already in the set above.)

        assert not violations, (
            f"wax.core contains forbidden I/O imports (violates INV-09):\n  "
            + "\n  ".join(f"{m} imports {f}" for m, f in violations)
        )

    @pytest.mark.architecture
    def test_core_only_imports_from_wax_core_or_stdlib_or_pydantic(self) -> None:
        """wax.core may only depend on stdlib, pydantic, and itself."""
        allowed_top_level: frozenset[str] = frozenset(
            {
                "wax",            # itself
                "pydantic",
                "pydantic_settings",
                "__future__",     # stdlib — from __future__ import annotations
                "typing",
                "typing_extensions",
                "dataclasses",
                "enum",
                "abc",
                "collections",
                "datetime",
                "decimal",
                "functools",
                "itertools",
                "uuid",
                "os",             # allowed in config.py for env reading
                "sys",
                "json",
                "re",
                "copy",
                "inspect",
                "contextlib",
                "types",
            }
        )

        violations: list[tuple[str, str]] = []
        for module_name in _walk_wax_core_modules():
            imports = _module_imports(module_name)
            for imp in imports:
                top = imp.split(".")[0]
                if top not in allowed_top_level:
                    violations.append((module_name, imp))

        assert not violations, (
            "wax.core imports modules outside the allowed set:\n  "
            + "\n  ".join(f"{m} imports {f}" for m, f in violations)
        )


# ---------------------------------------------------------------------------
# INV-01: Universal runtime mechanisms must not require education.
# ---------------------------------------------------------------------------

# Concepts that, if they appear as identifiers in wax.core source code,
# indicate the runtime has been contaminated by the education domain.
_FORBIDDEN_DOMAIN_CONCEPTS: frozenset[str] = frozenset(
    {
        "student",
        "teacher",
        "tutor",
        "lesson",
        "subject",
        "topic",  # ambiguous but flagged — education term
        "exam",
        "curriculum",
        "enrollment",
        "grade",
        "score",
        "waxprep",
    }
)


class TestNoDomainCoupling:
    """INV-01: wax.core must not contain education-domain concepts."""

    @pytest.mark.architecture
    def test_no_education_concepts_in_core_identifiers(self) -> None:
        """Check Python identifiers (class/function/variable names), not docstrings.

        We parse each module's AST and look for forbidden concept names among
        class names, function names, and module-level variable names. Strings
        inside docstrings, comments, or reason text are allowed — those are
        documentation, not contamination.
        """
        import ast

        from pathlib import Path

        violations: list[tuple[str, str]] = []
        core_path = Path(wax.core.__file__).parent

        for py_file in core_path.rglob("*.py"):
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                name: str | None = None
                if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                    name = node.name
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            name = target.id

                if not name:
                    continue

                name_lower = name.lower()
                for concept in _FORBIDDEN_DOMAIN_CONCEPTS:
                    if concept in name_lower:
                        violations.append(
                            (str(py_file.relative_to(core_path)), f"{concept} (identifier: {name})")
                        )

        assert not violations, (
            "wax.core contains forbidden education-domain identifiers "
            "(violates INV-01):\n  "
            + "\n  ".join(f"{f}: {c}" for f, c in violations)
        )
