"""
flhb-tool - Unit tests for Phase 1 (system_check) and Phase 3 (log_monitor).

Run with:
    python -m pytest tests/
or
    python -m unittest tests.test_system_check tests.test_log_monitor
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

# Allow `python -m unittest tests.test_*` from the project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flhb_tool.config import BridgeConfig, ConfigError, load_config
from flhb_tool.log_monitor import (
    CrashDump,
    ErrorDictionary,
    ErrorMatch,
    ErrorSignature,
    LogMonitor,
    default_error_signatures,
)
from flhb_tool.system_check import (
    check_cpu_virtualization,
    check_dnf_package,
    check_system_dependencies,
    check_vulkan_device,
)


class TestSystemCheck(unittest.TestCase):
    """Tests for Phase 1 dependency validation."""

    def test_check_dnf_package_unknown(self):
        """A made-up package should not be installed."""
        self.assertFalse(check_dnf_package("__flhb_nonexistent_package_xyz__"))

    def test_check_system_dependencies_missing_package(self):
        """A missing package should be reported with fix instructions."""
        report = check_system_dependencies(
            required_packages=["__flhb_nonexistent_package_xyz__"],
            check_vulkan=False,
            check_virtualization=False,
        )
        self.assertFalse(report.all_passed)
        self.assertIn("__flhb_nonexistent_package_xyz__", report.missing_packages)
        self.assertTrue(any("sudo dnf install" in i for i in report.fix_instructions))

    def test_check_cpu_virtualization_returns_tuple(self):
        """check_cpu_virtualization should return (bool, str)."""
        ok, detail = check_cpu_virtualization()
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(detail, str)

    def test_check_vulkan_device_returns_tuple(self):
        """check_vulkan_device should return (bool, str)."""
        ok, detail = check_vulkan_device()
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(detail, str)


class TestConfig(unittest.TestCase):
    """Tests for Phase 2 configuration schema."""

    def test_load_valid_config(self):
        """A valid config.json should load into BridgeConfig."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(
                '{"wine_prefix_path": "/pfx", '
                '"target_executable": "/game/bin/game.exe", '
                '"loader_arguments": ["--a", "b"], '
                '"environment_overrides": {"DXVK": "1"}}'
            )
            path = f.name
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.wine_prefix_path, "/pfx")
            self.assertEqual(cfg.target_executable, "/game/bin/game.exe")
            self.assertEqual(cfg.loader_arguments, ["--a", "b"])
            self.assertEqual(cfg.environment_overrides["DXVK"], "1")
            self.assertEqual(cfg.working_directory, "/game/bin")
        finally:
            os.unlink(path)

    def test_load_missing_file(self):
        """Loading a nonexistent config should raise ConfigError."""
        with self.assertRaises(ConfigError):
            load_config("/__flhb_nonexistent_config__.json")

    def test_load_missing_required_field(self):
        """Missing required fields should raise ConfigError."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write('{"wine_prefix_path": "/pfx"}')  # missing target_executable
            path = f.name
        try:
            with self.assertRaises(ConfigError):
                load_config(path)
        finally:
            os.unlink(path)

    def test_working_directory_defaults_to_exe_dir(self):
        """working_directory should default to dirname(target_executable)."""
        cfg = BridgeConfig(
            wine_prefix_path="/pfx",
            target_executable="/games/foo/bin/game.exe",
        )
        self.assertEqual(cfg.working_directory, "/games/foo/bin")


class TestErrorDictionary(unittest.TestCase):
    """Tests for Phase 3 error signature matching."""

    def setUp(self):
        self.ed = ErrorDictionary()

    def test_default_signatures_nonempty(self):
        """Built-in dictionary should contain signatures."""
        self.assertGreater(len(self.ed.signatures), 0)

    def test_missing_dll_match(self):
        """import_dll errors should match missing_dll category."""
        sig = self.ed.match("err:module:import_dll Library foo.dll failed to load")
        self.assertIsNotNone(sig)
        self.assertEqual(sig.category, "missing_dll")

    def test_cpp_runtime_match(self):
        """MSVCP DLL errors should match cpp_runtime category."""
        sig = self.ed.match("MSVCP140.dll not found")
        self.assertIsNotNone(sig)
        self.assertEqual(sig.category, "cpp_runtime")

    def test_vulkan_init_match(self):
        """Vulkan init failures should match vulkan_init category."""
        sig = self.ed.match("failed to create Vulkan instance")
        self.assertIsNotNone(sig)
        self.assertEqual(sig.category, "vulkan_init")

    def test_access_violation_match(self):
        """Page faults should match access_violation category."""
        sig = self.ed.match("unhandled page fault at 0xdeadbeef")
        self.assertIsNotNone(sig)
        self.assertEqual(sig.category, "access_violation")

    def test_no_match_for_clean_line(self):
        """Clean output should not match any signature."""
        self.assertIsNone(self.ed.match("game started successfully"))
        self.assertIsNone(self.ed.match("loading level 1..."))

    def test_add_custom_signature(self):
        """Custom signatures should be addable and matchable."""
        self.ed.add(
            ErrorSignature(
                pattern=r"flhb_custom_error",
                category="custom",
                severity="error",
                description="A custom error.",
                fix_instruction="Do something.",
            )
        )
        sig = self.ed.match("flhb_custom_error happened")
        self.assertIsNotNone(sig)
        self.assertEqual(sig.category, "custom")


class TestLogMonitor(unittest.TestCase):
    """Tests for Phase 3 log monitoring and crash dumps."""

    def test_process_line_records_match(self):
        """process_line should record matches and return ErrorMatch."""
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            log_path = f.name
        try:
            monitor = LogMonitor(log_file=log_path)
            match = monitor.process_line(
                "stderr", "err:module:import_dll Library x.dll failed to load"
            )
            self.assertIsInstance(match, ErrorMatch)
            self.assertEqual(match.signature.category, "missing_dll")
            self.assertEqual(len(monitor.matches), 1)
            # Log file should contain the line.
            with open(log_path) as lf:
                self.assertIn("x.dll", lf.read())
        finally:
            os.unlink(log_path)

    def test_process_line_no_match(self):
        """Clean lines should not produce matches."""
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            log_path = f.name
        try:
            monitor = LogMonitor(log_file=log_path)
            match = monitor.process_line("stdout", "game running normally")
            self.assertIsNone(match)
            self.assertEqual(len(monitor.matches), 0)
        finally:
            os.unlink(log_path)

    def test_crash_dump_to_text(self):
        """CrashDump.to_text should contain key fields."""
        dump = CrashDump(
            timestamp="2026-01-01T00:00:00Z",
            exit_code=1,
            command="/usr/bin/wine game.exe",
            cwd="/games/foo",
            wine_prefix="/pfx",
            env_snapshot={"WINEPREFIX": "/pfx"},
            error_matches=[],
            last_stdout=[],
            last_stderr=["err: something failed"],
            fix_instructions=["sudo dnf install wine-core"],
        )
        text = dump.to_text()
        self.assertIn("Crash Dump Summary", text)
        self.assertIn("Exit code:     1", text)
        self.assertIn("something failed", text)
        self.assertIn("sudo dnf install wine-core", text)


class TestBridgeLaunch(unittest.TestCase):
    """Tests for Phase 2 process spawning and output streaming."""

    def test_launch_and_stream(self):
        """Launching a process should stream stdout and stderr."""
        from flhb_tool.bridge import GameBridge

        cfg = BridgeConfig(
            wine_prefix_path="/tmp/flhb-test-prefix",
            target_executable="/usr/bin/python3",
            loader_arguments=[
                "-c",
                "import sys; print('OUT'); print('ERR', file=sys.stderr)",
            ],
            working_directory="/tmp",
        )
        bridge = GameBridge(cfg)
        lines = []
        bridge.stream_to_callback(lambda s, line: lines.append((s, line)))
        rc = bridge.wait()
        self.assertEqual(rc, 0)
        self.assertEqual(len(lines), 2)
        streams = {s for s, _ in lines}
        self.assertIn("stdout", streams)
        self.assertIn("stderr", streams)
        # No double prefix.
        for _, line in lines:
            self.assertFalse(line.startswith("[stdout] [stdout]"))
            self.assertFalse(line.startswith("[stderr] [stderr]"))


if __name__ == "__main__":
    unittest.main()
