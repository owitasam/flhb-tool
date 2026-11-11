"""Fedora Lutris Loader Bridge (flhb-tool).

A Python-based orchestration and wrapper utility that integrates with
Lutris on Fedora Linux. Manages environment variables, validates system
dependencies, intercepts execution calls, and safely orchestrates
user-space loaders/bypasses alongside Wine/Proton prefixes.

Phase 1: Environment & Dependency Validation Module.
"""

from .system_check import (
    check_system_dependencies,
    check_dnf_package,
    check_vulkan_device,
    check_cpu_virtualization,
    SystemCheckReport,
    DependencyCheckResult,
    print_report,
    main as system_check_main,
)
from .config import (
    BridgeConfig,
    ConfigError,
    load_config,
    write_example_config,
)
from .bridge import (
    GameBridge,
    LaunchResult,
    main as bridge_main,
)
from .log_monitor import (
    CrashDump,
    ErrorDictionary,
    ErrorMatch,
    ErrorSignature,
    LogMonitor,
    default_error_signatures,
    monitor_bridge_with_log,
)

__all__ = [
    # Phase 1
    "check_system_dependencies",
    "check_dnf_package",
    "check_vulkan_device",
    "check_cpu_virtualization",
    "SystemCheckReport",
    "DependencyCheckResult",
    "print_report",
    "system_check_main",
    # Phase 2
    "BridgeConfig",
    "ConfigError",
    "load_config",
    "write_example_config",
    "GameBridge",
    "LaunchResult",
    "bridge_main",
    # Phase 3
    "CrashDump",
    "ErrorDictionary",
    "ErrorMatch",
    "ErrorSignature",
    "LogMonitor",
    "default_error_signatures",
    "monitor_bridge_with_log",
]

__version__ = "0.3.0"
