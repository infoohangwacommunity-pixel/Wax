"""Architectural invariant tests — prove the old architecture is GONE.

These tests prevent regression. If someone re-introduces a capability
registry, authority broker, resource accountant, isolation backend, etc.,
these tests fail.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


class TestNoRemovedSubsystems:
    """The removed subsystems must NOT be importable."""

    def test_no_authority_package(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("wax.authority")

    def test_no_capabilities_package(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("wax.capabilities")

    def test_no_resources_package(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("wax.resources")

    def test_no_isolation_package(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("wax.isolation")

    def test_no_objective_package(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("wax.objective")

    def test_no_security_package(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("wax.security")

    def test_no_control_plane(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("wax.runtime.control_plane")

    def test_no_leadership(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("wax.runtime.leadership")

    def test_no_provisioning(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("wax.runtime.provisioning")

    def test_no_blob_store(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("wax.runtime.blob_store")

    def test_no_isolation_runtime(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("wax.runtime.isolation_runtime")

    def test_no_metrics(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("wax.observability.metrics")


class TestNoRemovedSymbols:
    """The removed symbols must NOT appear in the source tree."""

    def test_no_capability_registry_references(self) -> None:
        src_root = Path(__file__).resolve().parent.parent.parent / "src" / "wax"
        for py_file in src_root.rglob("*.py"):
            content = py_file.read_text()
            assert "capability_registry" not in content, f"capability_registry in {py_file}"
            assert "CapabilityRegistry" not in content, f"CapabilityRegistry in {py_file}"
            assert "CapabilityDescriptor" not in content, f"CapabilityDescriptor in {py_file}"

    def test_no_authority_references(self) -> None:
        src_root = Path(__file__).resolve().parent.parent.parent / "src" / "wax"
        for py_file in src_root.rglob("*.py"):
            content = py_file.read_text()
            assert "ApprovalGate" not in content, f"ApprovalGate in {py_file}"
            assert "authority_broker" not in content, f"authority_broker in {py_file}"
            assert "AuthorizationService" not in content, f"AuthorizationService in {py_file}"

    def test_no_resource_accountant_references(self) -> None:
        src_root = Path(__file__).resolve().parent.parent.parent / "src" / "wax"
        for py_file in src_root.rglob("*.py"):
            content = py_file.read_text()
            assert "ResourceAccountant" not in content, f"ResourceAccountant in {py_file}"
            assert "resource_accountant" not in content, f"resource_accountant in {py_file}"

    def test_no_isolation_references(self) -> None:
        src_root = Path(__file__).resolve().parent.parent.parent / "src" / "wax"
        for py_file in src_root.rglob("*.py"):
            content = py_file.read_text()
            assert "namespace_boundary" not in content, f"namespace_boundary in {py_file}"
            assert "subprocess_boundary" not in content, f"subprocess_boundary in {py_file}"
            assert "isolation_runtime" not in content, f"isolation_runtime in {py_file}"


class TestTerminalExecutorExists:
    """The new terminal executor must exist and be importable."""

    def test_executor_importable(self) -> None:
        from wax.runtime.executor import TerminalExecutor, TerminalResult

        assert TerminalExecutor is not None
        assert TerminalResult is not None

    def test_executor_has_execute_method(self) -> None:
        from wax.runtime.executor import TerminalExecutor

        assert hasattr(TerminalExecutor, "execute")
        assert hasattr(TerminalExecutor, "execute_detached")
        assert hasattr(TerminalExecutor, "cleanup_detached")


class TestBridgeHasTerminalLoop:
    """The bridge must use the terminal executor, not a capability registry."""

    def test_bridge_imports_executor(self) -> None:
        from wax.runtime.bridge import service

        assert hasattr(service, "TerminalExecutor")
        assert hasattr(service, "TERMINAL_TOOL_SPEC")

    def test_bridge_has_intelligence_loop(self) -> None:
        from wax.runtime.bridge.service import RuntimeBridge

        assert hasattr(RuntimeBridge, "_run_intelligence_loop")
        assert hasattr(RuntimeBridge, "_execute_terminal")


class TestConfigHasTerminalSettings:
    """The config must have terminal settings, not old-subsystem settings."""

    def test_terminal_settings_present(self) -> None:
        from wax.core.config import settings_for_testing

        s = settings_for_testing()
        assert hasattr(s, "terminal_timeout_seconds")
        assert hasattr(s, "terminal_output_max_chars")
        assert hasattr(s, "terminal_working_dir_root")
        assert hasattr(s, "terminal_max_rounds")

    def test_old_settings_absent(self) -> None:
        from wax.core.config import settings_for_testing

        s = settings_for_testing()
        assert not hasattr(s, "isolation_backend")
        assert not hasattr(s, "provisioning_root")
        assert not hasattr(s, "snapshot_blob_root")
        assert not hasattr(s, "control_plane_token")
        assert not hasattr(s, "approval_expiry_seconds")
        assert not hasattr(s, "capability_idempotency_claim_seconds")
        assert not hasattr(s, "acquisition_allowed_hosts")
