"""
flhb-tool - Phase 3: Automated Log Monitoring & Error Remediation.

Captures runtime output from the Wine/loader execution stream, scans it in
real time for known failure signatures, maps each match to automated fixes
or descriptive user prompts, and writes clean exit codes plus crash-dump
summaries to a local ``flhb_debug.log`` file.

Designed to consume the ``(stream, line)`` tuples produced by
:class:`flhb_tool.bridge.GameBridge.iter_output()`.
"""

from __future__ import annotations

import collections
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Pattern, Tuple

logger = logging.getLogger("flhb-tool.log_monitor")

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ErrorSignature:
    """A single known-failure signature and its remediation guidance."""

    pattern: str
    category: str  # e.g. "missing_dll", "cpp_runtime", "vulkan_init", "access_violation"
    severity: str  # "error" | "warning" | "critical"
    description: str
    fix_instruction: str
    auto_fix: bool = False
    auto_fix_command: Optional[str] = None  # shell command to attempt, if any

    @property
    def compiled(self) -> Pattern[str]:
        return re.compile(self.pattern, re.IGNORECASE)


@dataclass
class ErrorMatch:
    """A matched error signature with the offending line and context."""

    signature: ErrorSignature
    line: str
    stream: str
    timestamp: str
    match_groups: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "stream": self.stream,
            "category": self.signature.category,
            "severity": self.signature.severity,
            "description": self.signature.description,
            "pattern": self.signature.pattern,
            "matched_line": self.line,
            "match_groups": self.match_groups,
            "fix_instruction": self.signature.fix_instruction,
            "auto_fix": self.signature.auto_fix,
            "auto_fix_command": self.signature.auto_fix_command,
        }


@dataclass
class CrashDump:
    """Structured crash-dump summary written to flhb_debug.log on failure."""

    timestamp: str
    exit_code: Optional[int]
    command: str
    cwd: str
    wine_prefix: str
    env_snapshot: Dict[str, str] = field(default_factory=dict)
    error_matches: List[ErrorMatch] = field(default_factory=list)
    last_stdout: List[str] = field(default_factory=list)
    last_stderr: List[str] = field(default_factory=list)
    fix_instructions: List[str] = field(default_factory=list)

    def to_text(self) -> str:
        lines: List[str] = []
        lines.append("=" * 72)
        lines.append("flhb-tool - Crash Dump Summary")
        lines.append("=" * 72)
        lines.append(f"Timestamp:     {self.timestamp}")
        lines.append(f"Exit code:     {self.exit_code}")
        lines.append(f"Command:       {self.command}")
        lines.append(f"Working dir:   {self.cwd}")
        lines.append(f"Wine prefix:   {self.wine_prefix}")
        lines.append("")

        if self.env_snapshot:
            lines.append("Environment snapshot (relevant vars):")
            for k in sorted(self.env_snapshot):
                lines.append(f"  {k}={self.env_snapshot[k]}")
            lines.append("")

        if self.error_matches:
            lines.append(f"Detected error signatures ({len(self.error_matches)}):")
            for idx, m in enumerate(self.error_matches, 1):
                lines.append(f"  [{idx}] {m.signature.category} ({m.signature.severity}) - {m.signature.description}")
                lines.append(f"        stream={m.stream}  time={m.timestamp}")
                lines.append(f"        line: {m.line}")
                if m.match_groups:
                    lines.append(f"        groups: {m.match_groups}")
                lines.append(f"        fix: {m.signature.fix_instruction}")
                if m.signature.auto_fix_command:
                    lines.append(f"        auto-fix command: {m.signature.auto_fix_command}")
            lines.append("")

        if self.last_stderr:
            lines.append("Last stderr lines:")
            for line in self.last_stderr:
                lines.append(f"  [stderr] {line}")
            lines.append("")

        if self.last_stdout:
            lines.append("Last stdout lines:")
            for line in self.last_stdout:
                lines.append(f"  [stdout] {line}")
            lines.append("")

        if self.fix_instructions:
            lines.append("Recommended fix instructions:")
            for instr in self.fix_instructions:
                lines.append(f"  - {instr}")

        lines.append("=" * 72)
        lines.append("End of crash dump")
        lines.append("=" * 72)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Default error dictionary
# ---------------------------------------------------------------------------


def default_error_signatures() -> List[ErrorSignature]:
    """Built-in dictionary of common Wine/loader failure signatures."""

    return [
        # ---- Missing DLLs -------------------------------------------------
        ErrorSignature(
            pattern=r"err:module:import_dll Library\s+([^\s]+)",
            category="missing_dll",
            severity="error",
            description="Wine failed to load a required DLL (import_dll).",
            fix_instruction=(
                "Install the missing DLL with winetricks, e.g. "
                "winetricks <dllname>, or copy the DLL into the Wine prefix "
                "system32/syswow64 directory."
            ),
            auto_fix=False,
        ),
        ErrorSignature(
            pattern=r"(?:Library|module)\s+([^\s]+\.dll)\s+(?:not found|failed to load|cannot load)",
            category="missing_dll",
            severity="error",
            description="A required .dll library could not be located or loaded.",
            fix_instruction=(
                "Install the missing DLL via winetricks or obtain a native "
                "copy and place it in the Wine prefix."
            ),
            auto_fix=False,
        ),
        ErrorSignature(
            pattern=r"err:module:LdrInitializeThunk.*(?:failed|error)",
            category="missing_dll",
            severity="critical",
            description="Wine DLL initialization failed during process startup.",
            fix_instruction=(
                "Check that all required DLLs are present and that the Wine "
                "prefix is not corrupted. Try reinstalling the prefix."
            ),
            auto_fix=False,
        ),
        # ---- C++ runtimes / Visual C++ redistributables -------------------
        ErrorSignature(
            pattern=r"(?:MSVCP|msvcrt|VCRUNTIME|concrt|api-ms-win-crt)\d*\.dll",
            category="cpp_runtime",
            severity="error",
            description="Missing Microsoft Visual C++ runtime DLL.",
            fix_instruction=(
                "Install the Visual C++ redistributable, e.g. "
                "winetricks vcrun2019 vcrun2015 vcrun2013, or download the "
                "official Microsoft redistributable and run it inside the prefix."
            ),
            auto_fix=False,
            auto_fix_command="winetricks vcrun2019",
        ),
        ErrorSignature(
            pattern=r"err:ole:CoCreateInstance.*(?:vcrun|msvc|Visual C)",
            category="cpp_runtime",
            severity="error",
            description="COM creation failed, likely due to missing C++ runtime.",
            fix_instruction=(
                "Install the Visual C++ redistributable via winetricks "
                "(vcrun2019/vcrun2015/vcrun2013)."
            ),
            auto_fix=False,
        ),
        # ---- Vulkan initialization ---------------------------------------
        ErrorSignature(
            pattern=r"vkCreateInstance|vkEnumeratePhysicalDevices|vkGetInstanceProcAddr",
            category="vulkan_init",
            severity="warning",
            description="Vulkan function called; may indicate initialization.",
            fix_instruction=(
                "Ensure vulkan-loader and mesa-vulkan-drivers are installed: "
                "sudo dnf install -y vulkan-loader mesa-vulkan-drivers"
            ),
            auto_fix=False,
        ),
        ErrorSignature(
            pattern=r"(?:failed to create|unable to create|could not initialize).{0,40}?[Vv]ulkan",
            category="vulkan_init",
            severity="critical",
            description="Vulkan instance/device initialization failed.",
            fix_instruction=(
                "Reinstall Vulkan drivers and confirm a GPU is detected: "
                "sudo dnf install -y vulkan-loader mesa-vulkan-drivers && vulkaninfo"
            ),
            auto_fix=False,
        ),
        ErrorSignature(
            pattern=r"(?:DXVK|dxvk).{0,60}?(?:error|failed|failed to initialize|not supported)",
            category="vulkan_init",
            severity="error",
            description="DXVK (DirectX-to-Vulkan translation) encountered an error.",
            fix_instruction=(
                "Update DXVK in the Wine prefix or disable it by setting "
                "DXVK=0 in environment_overrides."
            ),
            auto_fix=False,
        ),
        # ---- Access violations / crashes ---------------------------------
        ErrorSignature(
            pattern=r"(?:unhandled page fault|page fault|access violation|segmentation fault)",
            category="access_violation",
            severity="critical",
            description="Memory access violation / crash in Wine or the application.",
            fix_instruction=(
                "This is often a Wine bug or a missing dependency. Try a clean "
                "Wine prefix, update Wine, or run with WINEDEBUG=+relay to capture "
                "the failing instruction."
            ),
            auto_fix=False,
        ),
        ErrorSignature(
            pattern=r"wine: Unhandled exception",
            category="access_violation",
            severity="critical",
            description="Wine raised an unhandled exception.",
            fix_instruction=(
                "Capture a full backtrace with WINEDEBUG=+all and check the "
                "application's compatibility notes on the Lutris/WineHQ database."
            ),
            auto_fix=False,
        ),
        ErrorSignature(
            pattern=r"wine: cannot find\s+(.+)",
            category="missing_binary",
            severity="error",
            description="Wine could not find the referenced executable/binary.",
            fix_instruction=(
                "Verify target_executable points to a valid binary inside the "
                "Wine prefix and that the file is executable (chmod +x)."
            ),
            auto_fix=False,
        ),
        # ---- General Wine errors -----------------------------------------
        ErrorSignature(
            pattern=r"err:.*",
            category="wine_error",
            severity="warning",
            description="Generic Wine error message.",
            fix_instruction=(
                "Review the full stderr output; search the error text on the "
                "WineHQ AppDB or Lutris issue tracker."
            ),
            auto_fix=False,
        ),
    ]


class ErrorDictionary:
    """Holds error signatures and provides lookup by compiled regex."""

    def __init__(self, signatures: Optional[List[ErrorSignature]] = None) -> None:
        self.signatures: List[ErrorSignature] = list(
            signatures or default_error_signatures()
        )
        self._compiled: List[Tuple[Pattern[str], ErrorSignature]] = [
            (sig.compiled, sig) for sig in self.signatures
        ]

    def add(self, signature: ErrorSignature) -> None:
        self.signatures.append(signature)
        self._compiled.append((signature.compiled, signature))

    def match(self, line: str) -> Optional[ErrorSignature]:
        """Return the first signature whose pattern matches *line*."""
        for regex, sig in self._compiled:
            if regex.search(line):
                return sig
        return None


# ---------------------------------------------------------------------------
# Log monitor
# ---------------------------------------------------------------------------


class LogMonitor:
    """Real-time log parser and error remediation coordinator.

    Consumes ``(stream, line)`` tuples (e.g. from
    :meth:`flhb_tool.bridge.GameBridge.iter_output`), writes everything to
    ``flhb_debug.log``, matches lines against the error dictionary, and
    produces a crash-dump summary on non-zero exit.
    """

    def __init__(
        self,
        log_file: str = "flhb_debug.log",
        error_dict: Optional[ErrorDictionary] = None,
        auto_remediate: bool = False,
        max_tail_lines: int = 50,
    ) -> None:
        self.log_file = log_file
        self.error_dict = error_dict or ErrorDictionary()
        self.auto_remediate = auto_remediate
        self.max_tail_lines = max_tail_lines

        self.matches: List[ErrorMatch] = []
        self.last_stdout: collections.deque = collections.deque(maxlen=max_tail_lines)
        self.last_stderr: collections.deque = collections.deque(maxlen=max_tail_lines)
        self._fix_instructions: List[str] = []

        # Ensure the log file exists with a header.
        self._initialize_log()

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def _initialize_log(self) -> None:
        try:
            with open(self.log_file, "a", encoding="utf-8") as fh:
                fh.write(
                    f"\n{'='*72}\n"
                    f"flhb-tool log session started: {self._now()}\n"
                    f"{'='*72}\n"
                )
        except OSError as exc:
            logger.warning("Could not initialize log file %s: %s", self.log_file, exc)

    def _write_log(self, stream: str, line: str) -> None:
        try:
            with open(self.log_file, "a", encoding="utf-8") as fh:
                fh.write(f"[{self._now()}] [{stream}] {line}\n")
        except OSError as exc:
            logger.warning("Could not write to log file %s: %s", self.log_file, exc)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %Z")

    # ------------------------------------------------------------------
    # Line processing
    # ------------------------------------------------------------------
    def process_line(self, stream: str, line: str) -> Optional[ErrorMatch]:
        """Process one output line: log it, track tails, match errors.

        Returns an :class:`ErrorMatch` if a signature matched, else None.
        """
        self._write_log(stream, line)

        if stream == "stdout":
            self.last_stdout.append(line)
        else:
            self.last_stderr.append(line)

        signature = self.error_dict.match(line)
        if signature is None:
            return None

        match = ErrorMatch(
            signature=signature,
            line=line,
            stream=stream,
            timestamp=self._now(),
            match_groups=self._capture_groups(signature, line),
        )
        self.matches.append(match)

        instr = f"[{signature.category}] {signature.fix_instruction}"
        if instr not in self._fix_instructions:
            self._fix_instructions.append(instr)

        log_level = {
            "critical": logging.CRITICAL,
            "error": logging.ERROR,
            "warning": logging.WARNING,
        }.get(signature.severity, logging.WARNING)

        logger.log(
            log_level,
            "Error signature matched [%s/%s]: %s | line: %s",
            signature.category,
            signature.severity,
            signature.description,
            line,
        )

        if self.auto_remediate and signature.auto_fix_command:
            self._attempt_auto_fix(signature)

        return match

    @staticmethod
    def _capture_groups(signature: ErrorSignature, line: str) -> Tuple[str, ...]:
        try:
            m = signature.compiled.search(line)
            if m:
                return tuple(g for g in m.groups() if g is not None)
        except re.error:
            pass
        return ()

    def _attempt_auto_fix(self, signature: ErrorSignature) -> None:
        if not signature.auto_fix_command:
            return
        cmd = signature.auto_fix_command
        logger.info("Attempting auto-fix: %s", cmd)
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode == 0:
                logger.info("Auto-fix succeeded: %s", cmd)
            else:
                logger.warning(
                    "Auto-fix failed (rc=%s): %s\nstderr: %s",
                    proc.returncode,
                    cmd,
                    proc.stderr.strip()[:200],
                )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("Auto-fix errored: %s -> %s", cmd, exc)

    # ------------------------------------------------------------------
    # Bridge monitoring
    # ------------------------------------------------------------------
    def monitor_bridge(
        self,
        bridge,
        on_line: Optional[Callable[[str, str], None]] = None,
    ) -> int:
        """Iterate a launched :class:`GameBridge`, processing every line.

        Args:
            bridge: A launched :class:`GameBridge`.
            on_line: Optional callback invoked with every ``(stream, line)``
                as it arrives (useful for real-time display to the user).

        Returns the process exit code. After the process exits, writes a
        crash dump to ``log_file`` if the exit code was non-zero or any
        errors were matched.
        """
        exit_code: Optional[int] = None
        try:
            # Auto-launch if the bridge hasn't been started yet.
            if bridge.process is None:
                bridge.launch()
            for stream, line in bridge.iter_output():
                self.process_line(stream, line)
                if on_line is not None:
                    try:
                        on_line(stream, line)
                    except Exception as exc:  # noqa: BLE001 - user callback
                        logger.exception("Output callback raised: %s", exc)
            exit_code = bridge.wait()
        finally:
            # Ensure reader threads are joined / process reaped.
            if bridge.process is not None and bridge.process.poll() is None:
                bridge.terminate()
                try:
                    bridge.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    bridge.kill()
            if exit_code is None:
                exit_code = bridge.poll()

        self._write_crash_dump(exit_code, bridge)
        return exit_code if exit_code is not None else 1

    # ------------------------------------------------------------------
    # Crash dump
    # ------------------------------------------------------------------
    def _write_crash_dump(
        self, exit_code: Optional[int], bridge
    ) -> None:
        should_dump = (exit_code not in (None, 0)) or bool(self.matches)
        if not should_dump:
            logger.info(
                "Process exited cleanly (rc=%s); no crash dump written.",
                exit_code,
            )
            return

        config = getattr(bridge, "config", None)
        wine_prefix = getattr(config, "wine_prefix_path", "unknown") if config else "unknown"
        cwd = getattr(config, "working_directory", "unknown") if config else "unknown"
        command = " ".join(getattr(bridge, "build_command", lambda: [])())

        env_snapshot: Dict[str, str] = {}
        for key in ("WINEPREFIX", "WINEARCH", "DXVK", "VKD3D", "WINEDEBUG", "WINEESYNC", "WINEFSYNC"):
            val = os.environ.get(key)
            if val is not None:
                env_snapshot[key] = val

        dump = CrashDump(
            timestamp=self._now(),
            exit_code=exit_code,
            command=command,
            cwd=cwd,
            wine_prefix=wine_prefix,
            env_snapshot=env_snapshot,
            error_matches=list(self.matches),
            last_stdout=list(self.last_stdout),
            last_stderr=list(self.last_stderr),
            fix_instructions=list(self._fix_instructions),
        )

        dump_text = dump.to_text()
        logger.critical("Writing crash dump to %s", self.log_file)
        try:
            with open(self.log_file, "a", encoding="utf-8") as fh:
                fh.write("\n" + dump_text + "\n")
        except OSError as exc:
            logger.error("Could not write crash dump to %s: %s", self.log_file, exc)

        # Also print a concise summary to stderr for the user/Lutris.
        print("\n" + "=" * 72, file=sys.stderr)
        print("flhb-tool: process exited with code", exit_code, file=sys.stderr)
        if self.matches:
            print("Errors detected:", file=sys.stderr)
            for m in self.matches[:5]:
                print(f"  - [{m.signature.category}] {m.signature.description}", file=sys.stderr)
        if self._fix_instructions:
            print("Fix instructions:", file=sys.stderr)
            for instr in self._fix_instructions[:5]:
                print(f"  - {instr}", file=sys.stderr)
        print("Full crash dump written to:", self.log_file, file=sys.stderr)
        print("=" * 72, file=sys.stderr)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def generate_report(self) -> str:
        """Return a human-readable summary of the monitoring session."""
        lines: List[str] = []
        lines.append("flhb-tool Log Monitor Report")
        lines.append("-" * 40)
        lines.append(f"Log file:        {self.log_file}")
        lines.append(f"Error matches:   {len(self.matches)}")
        if self.matches:
            lines.append("")
            lines.append("Matches:")
            for m in self.matches:
                lines.append(
                    f"  [{m.timestamp}] [{m.stream}] {m.signature.category} ({m.signature.severity}): "
                    f"{m.signature.description}"
                )
        if self._fix_instructions:
            lines.append("")
            lines.append("Fix instructions:")
            for instr in self._fix_instructions:
                lines.append(f"  - {instr}")
        return "\n".join(lines)


def monitor_bridge_with_log(
    bridge,
    log_file: str = "flhb_debug.log",
    auto_remediate: bool = False,
) -> int:
    """Convenience: run a bridge and monitor its output with LogMonitor.

    Returns the process exit code.
    """
    monitor = LogMonitor(log_file=log_file, auto_remediate=auto_remediate)
    return monitor.monitor_bridge(bridge)
