"""
flhb-tool - Phase 1: Environment & Dependency Validation Module.

Provides pre-flight system checks to ensure the Fedora host meets all
runtime requirements before launching a game or loader.

Checks performed:
  1. Presence of required system packages via rpm/dnf (e.g. wine-core,
     vulkan-loader, mesa-vulkan-drivers).
  2. Active Vulkan device availability using vulkaninfo, falling back to
     parsing /sys/class/drm/.
  3. CPU virtualization flags (vmx/svm) in /proc/cpuinfo when
     kernel-level features are referenced.

Missing dependencies are logged with explicit fix instructions (e.g.
`sudo dnf install <package>`).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

logger = logging.getLogger("flhb-tool.system_check")

# Default runtime requirements for a Fedora + Lutris + Wine/Proton host.
DEFAULT_REQUIRED_PACKAGES: List[str] = [
    "wine-core",
    "vulkan-loader",
    "mesa-vulkan-drivers",
]


@dataclass
class DependencyCheckResult:
    """Result of a single system package check."""

    package: str
    installed: bool
    fix_command: str


@dataclass
class SystemCheckReport:
    """Aggregated result of all pre-flight system checks."""

    all_passed: bool = True
    package_results: List[DependencyCheckResult] = field(default_factory=list)
    vulkan_available: bool = False
    vulkan_detail: str = ""
    virtualization_available: bool = False
    virtualization_detail: str = ""
    missing_packages: List[str] = field(default_factory=list)
    fix_instructions: List[str] = field(default_factory=list)


def _run(cmd: List[str], timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a command safely, returning the CompletedProcess or a dummy on failure."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        # Return a fake failed process so callers can uniformly inspect returncode.
        class _Dummy:
            returncode = 127
            stdout = ""
            stderr = str(exc)

        return _Dummy()  # type: ignore[return-value]


def check_dnf_package(package_name: str) -> bool:
    """Return True if *package_name* is installed on the Fedora host.

    Uses ``rpm -q`` first (fast, no root required), then falls back to
    ``dnf list installed`` and ``dnf5 list installed``.
    """
    # Fast path: rpm query (works without root).
    result = _run(["rpm", "-q", package_name], timeout=10)
    if result.returncode == 0:
        return True

    # Fallbacks: dnf / dnf5.
    for dnf_cmd in ("dnf", "dnf5"):
        if not shutil.which(dnf_cmd):
            continue
        result = _run([dnf_cmd, "list", "installed", package_name], timeout=15)
        if result.returncode == 0 and package_name in result.stdout:
            return True

    return False


def check_vulkan_device() -> Tuple[bool, str]:
    """Check for an active Vulkan device.

    Prefers ``vulkaninfo --summary``; falls back to scanning
    ``/sys/class/drm/`` for DRM card nodes with vendor/device info.
    """
    # Primary: vulkaninfo.
    if shutil.which("vulkaninfo"):
        result = _run(["vulkaninfo", "--summary"], timeout=15)
        if result.returncode == 0 and ("GPU" in result.stdout or "Vulkan" in result.stdout):
            return True, "vulkaninfo reported active Vulkan GPU(s)"

    # Fallback: parse /sys/class/drm/.
    drm_path = "/sys/class/drm"
    if os.path.isdir(drm_path):
        try:
            for entry in sorted(os.listdir(drm_path)):
                entry_path = os.path.join(drm_path, entry)
                if not os.path.isdir(entry_path) or not entry.startswith("card"):
                    continue
                vendor_file = os.path.join(entry_path, "vendor")
                device_file = os.path.join(entry_path, "device")
                if os.path.exists(vendor_file) and os.path.exists(device_file):
                    return (
                        True,
                        f"Found DRM card '{entry}' with vendor/device nodes",
                    )
        except OSError as exc:
            logger.debug("Unable to scan %s: %s", drm_path, exc)

    return False, "No active Vulkan device detected"


def check_cpu_virtualization() -> Tuple[bool, str]:
    """Check /proc/cpuinfo for Intel (vmx) or AMD (svm) virtualization flags."""
    cpuinfo_path = "/proc/cpuinfo"
    if not os.path.exists(cpuinfo_path):
        return False, "/proc/cpuinfo not available"

    try:
        with open(cpuinfo_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        return False, f"Unable to read /proc/cpuinfo: {exc}"

    has_vmx = "vmx" in content
    has_svm = "svm" in content

    if has_vmx or has_svm:
        flags: List[str] = []
        if has_vmx:
            flags.append("vmx (Intel VT-x)")
        if has_svm:
            flags.append("svm (AMD-V)")
        return True, f"CPU virtualization flags found: {', '.join(flags)}"

    return False, "No vmx/svm virtualization flags found in /proc/cpuinfo"


def check_system_dependencies(
    required_packages: Optional[List[str]] = None,
    check_vulkan: bool = True,
    check_virtualization: bool = True,
) -> SystemCheckReport:
    """Verify the Fedora host system meets all runtime requirements.

    Args:
        required_packages: Package names to verify via rpm/dnf. Defaults to
            ``[wine-core, vulkan-loader, mesa-vulkan-drivers]``.
        check_vulkan: Whether to check for an active Vulkan device.
        check_virtualization: Whether to check CPU virtualization flags
            (vmx/svm) in /proc/cpuinfo.

    Returns:
        A :class:`SystemCheckReport` summarising every check. Missing
        dependencies are logged with explicit fix instructions.
    """
    if required_packages is None:
        required_packages = DEFAULT_REQUIRED_PACKAGES

    report = SystemCheckReport(all_passed=True)

    logger.info("flhb-tool: starting system dependency checks...")

    # ------------------------------------------------------------------
    # 1. Required system packages via rpm/dnf
    # ------------------------------------------------------------------
    for package in required_packages:
        installed = check_dnf_package(package)
        result = DependencyCheckResult(
            package=package,
            installed=installed,
            fix_command=f"sudo dnf install -y {package}",
        )
        report.package_results.append(result)

        if not installed:
            report.missing_packages.append(package)
            report.all_passed = False
            instruction = (
                f"Missing required package '{package}'. "
                f"Install with: {result.fix_command}"
            )
            report.fix_instructions.append(instruction)
            logger.error(instruction)
        else:
            logger.info("Package '%s' is installed.", package)

    # ------------------------------------------------------------------
    # 2. Active Vulkan device availability
    # ------------------------------------------------------------------
    if check_vulkan:
        vulkan_ok, vulkan_detail = check_vulkan_device()
        report.vulkan_available = vulkan_ok
        report.vulkan_detail = vulkan_detail
        if not vulkan_ok:
            report.all_passed = False
            instruction = (
                "No active Vulkan device detected. "
                "Install Vulkan drivers with: "
                "sudo dnf install -y vulkan-loader mesa-vulkan-drivers"
            )
            report.fix_instructions.append(instruction)
            logger.error(instruction)
        else:
            logger.info("Vulkan check: %s", vulkan_detail)

    # ------------------------------------------------------------------
    # 3. CPU virtualization flags (vmx/svm)
    # ------------------------------------------------------------------
    if check_virtualization:
        virt_ok, virt_detail = check_cpu_virtualization()
        report.virtualization_available = virt_ok
        report.virtualization_detail = virt_detail
        if not virt_ok:
            report.all_passed = False
            instruction = (
                f"CPU virtualization: {virt_detail}. "
                "If kernel-level features are required, enable VT-x/AMD-V "
                "in your system BIOS/UEFI."
            )
            report.fix_instructions.append(instruction)
            logger.warning(instruction)
        else:
            logger.info("Virtualization check: %s", virt_detail)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    if report.all_passed:
        logger.info("flhb-tool: all system dependency checks passed.")
    else:
        missing = ", ".join(report.missing_packages)
        logger.error(
            "flhb-tool: system dependency checks failed. "
            "Missing packages: %s. Review fix instructions above.",
            missing,
        )

    return report


def print_report(report: SystemCheckReport) -> None:
    """Print a human-readable summary of the check report to stdout."""
    line = "=" * 60
    print(line)
    print("flhb-tool - System Dependency Check Report")
    print(line)

    print("\n[System Packages]")
    for result in report.package_results:
        status = "OK" if result.installed else "MISSING"
        print(f"  [{status}] {result.package}")
        if not result.installed:
            print(f"         Fix: {result.fix_command}")

    print("\n[Vulkan Device]")
    status = "OK" if report.vulkan_available else "MISSING"
    print(f"  [{status}] {report.vulkan_detail}")

    print("\n[CPU Virtualization]")
    status = "OK" if report.virtualization_available else "WARNING"
    print(f"  [{status}] {report.virtualization_detail}")

    print("\n" + line)
    if report.all_passed:
        print("Result: ALL CHECKS PASSED")
    else:
        print("Result: CHECKS FAILED - review missing dependencies above")
        if report.fix_instructions:
            print("\nFix instructions:")
            for instr in report.fix_instructions:
                print(f"  - {instr}")
    print(line)


def setup_logging(
    level: int = logging.INFO,
    log_file: Optional[str] = None,
) -> None:
    """Configure logging for flhb-tool system checks.

    Args:
        level: Logging level (default INFO).
        log_file: Optional path to a log file (e.g. flhb_debug.log).
    """
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point for running system checks standalone.

    Returns 0 if all checks pass, 1 otherwise.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="flhb-check",
        description="flhb-tool Phase 1: pre-flight system dependency checks.",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Optional path to write structured logs (e.g. flhb_debug.log).",
    )
    parser.add_argument(
        "--no-vulkan",
        action="store_true",
        help="Skip the active Vulkan device check.",
    )
    parser.add_argument(
        "--no-virtualization",
        action="store_true",
        help="Skip the CPU virtualization (vmx/svm) check.",
    )
    parser.add_argument(
        "--package",
        action="append",
        default=None,
        help="Additional package to verify (can be repeated).",
    )
    args = parser.parse_args(argv)

    setup_logging(level=logging.INFO, log_file=args.log_file)

    required_packages = list(DEFAULT_REQUIRED_PACKAGES)
    if args.package:
        required_packages.extend(args.package)

    report = check_system_dependencies(
        required_packages=required_packages,
        check_vulkan=not args.no_vulkan,
        check_virtualization=not args.no_virtualization,
    )

    print_report(report)
    return 0 if report.all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
